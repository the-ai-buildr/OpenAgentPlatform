"""Request and response models for the OpenAI-compatible bridge."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileRouteRequest(BaseModel):
    filename_query: str = Field(min_length=1, max_length=512)
    agent_id: str | None = Field(default="platform-builder", max_length=128)
    target_folder: str | None = Field(default=None, max_length=512)
    auto_index: bool = True
    bind_to_agent: bool = True


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    """Subset of the OpenAI chat-completions request that this bridge honours."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    chat_id: str | None = None
    session_id: str | None = None


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "agno-agentos"
    name: str | None = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]
