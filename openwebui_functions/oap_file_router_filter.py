"""
title: OAP File Router
author: the-ai-buildr
version: 2.0.0
license: MIT
description: >-
  Routes files attached in Open WebUI into the matching OAP workspace
  folder. Only needed when OAP is wired up as a plain OpenAI connection;
  the OAP manifold Pipe and the bridge already handle routing.
requirements: requests
"""

# Install: Open WebUI -> Workspace -> Functions -> + -> paste this file -> Save.
#
# Leave this DISABLED if you use the OAP Pipe. Staging is idempotent
# (the bridge deduplicates by content hash), so running both is harmless but
# doubles the work on every upload.

import re
from typing import Any, Dict, List, Optional

import requests
from pydantic import BaseModel, Field

FOLDER_TAG = re.compile(r"#folder:(\S+)")


class Filter:
    class Valves(BaseModel):
        oap_base_url: str = Field(
            default="http://oap-api:8000",
            description="Base URL of the OAP API as seen from the Open WebUI container.",
        )
        api_key: str = Field(default="", description="OS_SECURITY_KEY for the OAP bridge.")
        default_folder: str = Field(
            default="",
            description="Folder to use when the message carries no #folder: tag. Blank = let OAP decide.",
        )
        auto_bind_agent_knowledge: bool = Field(
            default=True,
            description="Bind the destination folder to a custom agent that has none yet.",
        )
        request_timeout: int = Field(default=30, description="Per-file routing timeout in seconds.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.valves.api_key:
            headers["Authorization"] = f"Bearer {self.valves.api_key}"
        return headers

    @staticmethod
    def _folder_tag(messages: List[Dict[str, Any]]) -> Optional[str]:
        for message in reversed(messages or []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else ""
            match = FOLDER_TAG.search(text)
            if match:
                return match.group(1).rstrip(".,;")
            break
        return None

    @staticmethod
    def _filenames(files: Any) -> List[str]:
        names: List[str] = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("file") or {}
            name = item.get("name") or meta.get("filename") or meta.get("name") or item.get("id")
            if name:
                names.append(str(name))
        return names

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Any = None,
        __files__: Any = None,
        __user__: Any = None,
    ) -> Dict[str, Any]:
        names = self._filenames(__files__ or body.get("files"))
        if not names:
            return body

        folder = self._folder_tag(body.get("messages", [])) or self.valves.default_folder or None
        model = body.get("model") or "platform-builder"

        for name in names:
            await self._emit(__event_emitter__, f"Routing {name} to OAP…", done=False)
            try:
                response = requests.post(
                    f"{self.valves.oap_base_url.rstrip('/')}/v1/files/route",
                    headers=self._headers(),
                    json={
                        "filename_query": name,
                        "agent_id": model,
                        "target_folder": folder,
                        "auto_index": True,
                        "bind_to_agent": self.valves.auto_bind_agent_knowledge,
                    },
                    timeout=self.valves.request_timeout,
                )
                response.raise_for_status()
                result = response.json()
                state = "indexed" if result.get("indexed") else "stored"
                await self._emit(
                    __event_emitter__, f"{name} {state} in '{result.get('folder')}'", done=True
                )
            except Exception as exc:
                await self._emit(__event_emitter__, f"Could not route {name}: {exc}", done=True)

        return body

    @staticmethod
    async def _emit(emitter: Any, description: str, done: bool) -> None:
        if emitter is None:
            return
        try:
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            pass
