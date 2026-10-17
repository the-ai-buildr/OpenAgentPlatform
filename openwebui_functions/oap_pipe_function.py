"""
title: OAP
author: the-ai-buildr
version: 2.0.0
license: MIT
description: >-
  Manifold pipe that exposes every OAP agent and the coordinator team
  as Open WebUI models, streaming responses through the OAP /v1 bridge.
requirements: requests
"""

# Install: Open WebUI -> Workspace -> Functions -> + -> paste this file -> Save,
# then enable it and set the api_key valve to your OS_SECURITY_KEY.
#
# File routing is handled by the OAP bridge itself, so `auto_route_uploads`
# defaults to False. Turning it on makes this function route uploads too; the
# bridge deduplicates by content hash, so it is safe but redundant.

import json
from typing import Any, Dict, Generator, Iterator, List, Union

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        oap_base_url: str = Field(
            default="http://oap-api:8000",
            description="Base URL of the OAP API as seen from the Open WebUI container.",
        )
        api_key: str = Field(
            default="",
            description="OS_SECURITY_KEY. Required unless the bridge runs unauthenticated.",
        )
        request_timeout: int = Field(
            default=300, description="Seconds to wait for a completion before giving up."
        )
        auto_route_uploads: bool = Field(
            default=False,
            description=(
                "Route attached files from this function as well. The bridge already "
                "does this; leave off unless you have disabled bridge-side routing."
            ),
        )

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "oap"
        self.name = "OAP/"
        self.valves = self.Valves()

    # -- helpers ---------------------------------------------------------- #
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.valves.api_key:
            headers["Authorization"] = f"Bearer {self.valves.api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.valves.oap_base_url.rstrip('/')}{path}"

    def _strip_prefix(self, model_id: str) -> str:
        # Open WebUI prefixes manifold models with "<function id>."
        return model_id.split(".", 1)[1] if model_id.startswith(f"{self.id}.") else model_id

    # -- Open WebUI entry points ------------------------------------------ #
    def pipes(self) -> List[Dict[str, str]]:
        try:
            response = requests.get(self._url("/v1/models"), headers=self._headers(), timeout=15)
            response.raise_for_status()
            return [
                {"id": model["id"], "name": model.get("name") or model["id"]}
                for model in response.json().get("data", [])
            ]
        except Exception as exc:
            return [{"id": "error", "name": f"OAP unreachable: {exc}"}]

    def pipe(self, body: Dict[str, Any], __files__: Any = None, __user__: Any = None):
        model = self._strip_prefix(body.get("model", ""))

        if self.valves.auto_route_uploads:
            self._route_files(model, __files__ or body.get("files") or [])

        payload = {
            "model": model,
            "messages": body.get("messages", []),
            "stream": bool(body.get("stream")),
            "user": (__user__ or {}).get("id") if isinstance(__user__, dict) else None,
            "chat_id": body.get("chat_id"),
            "files": body.get("files") or [],
        }

        try:
            if payload["stream"]:
                return self._stream(payload)
            response = requests.post(
                self._url("/v1/chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.valves.request_timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            return f"OAP error ({exc.response.status_code if exc.response else '?'}): {detail}"
        except Exception as exc:
            return f"OAP error: {exc}"

    def _stream(self, payload: Dict[str, Any]) -> Union[Generator, Iterator]:
        with requests.post(
            self._url("/v1/chat/completions"),
            headers=self._headers(),
            json=payload,
            stream=True,
            timeout=self.valves.request_timeout,
        ) as response:
            response.raise_for_status()
            for raw in response.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data: "):
                    continue
                chunk = raw[len("data: ") :]
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
                if delta:
                    yield delta

    def _route_files(self, model: str, files: Any) -> None:
        for item in files or []:
            name = None
            if isinstance(item, dict):
                meta = item.get("file") or {}
                name = item.get("name") or meta.get("filename") or meta.get("name") or item.get("id")
            if not name:
                continue
            try:
                requests.post(
                    self._url("/v1/files/route"),
                    headers=self._headers(),
                    json={"filename_query": str(name), "agent_id": model},
                    timeout=60,
                )
            except Exception:
                # Routing is advisory; a failure must not block the chat turn.
                pass
