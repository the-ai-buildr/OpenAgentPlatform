"""Typed configuration for the AgentOS runtime.

Every value is sourced from the environment (see `.env.example`). Nothing in
here reads a file at import time other than the optional local `.env`, so the
module is safe to import from tests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Paths are fixed by the container layout and intentionally not configurable:
# the compose file binds host directories onto exactly these locations.
WORKSPACE_ROOT = Path("/app/workspace_files")
OPENWEBUI_UPLOADS_DIR = Path("/app/openwebui_data/uploads")
PROJECT_ROOT = Path("/app")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- LLM (OpenRouter) ---------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    agno_default_model: str = "anthropic/claude-sonnet-4.5"
    agno_builder_model: str = "anthropic/claude-sonnet-4.5"
    agno_engineer_model: str = "anthropic/claude-sonnet-4.5"
    agno_manager_model: str = "openai/gpt-4o-mini"
    agno_model_max_tokens: int = 4096

    # --- Embeddings ---------------------------------------------------------
    # OpenRouter serves no embedding models, so embeddings are configured
    # independently and default to the OpenAI API.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"

    # --- AgentOS runtime ----------------------------------------------------
    agentos_id: str = "agno-agentos-platform"
    agentos_port: int = 8000
    os_security_key: str = ""
    agno_telemetry: bool = False
    agno_debug: bool = False
    enable_mcp_server: bool = True

    # --- File watcher -------------------------------------------------------
    enable_file_watcher: bool = True
    watcher_debounce_seconds: float = 2.0

    # --- OpenTelemetry ------------------------------------------------------
    otel_enabled: bool = True
    otel_service_name: str = "agno-agentos-api"
    otel_environment: str = "development"
    # Base OTLP/HTTP collector URL. The `/v1/traces` suffix is appended by this
    # application, matching the OpenTelemetry specification for
    # OTEL_EXPORTER_OTLP_ENDPOINT.
    otel_exporter_otlp_endpoint: str = "http://agentos-otel:6006"
    otel_exporter_otlp_headers: str = ""

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://ai:ai@agentos-db:5432/agentos"

    # --- Bridge -------------------------------------------------------------
    max_staged_upload_bytes: int = 64 * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def otel_traces_endpoint(self) -> str:
        return self.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_configured(self) -> bool:
        return bool(self.openrouter_api_key.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def embeddings_configured(self) -> bool:
        return bool(self.effective_embedding_api_key)

    @property
    def effective_embedding_api_key(self) -> str:
        """Embedding key, falling back to OpenRouter only for OpenRouter-hosted URLs.

        OpenRouter does not expose an embeddings endpoint, so silently reusing
        the OpenRouter key against `api.openai.com` would send the wrong
        credential. The fallback therefore only applies when the embedding base
        URL really is OpenRouter (i.e. someone is proxying).
        """
        if self.embedding_api_key.strip():
            return self.embedding_api_key.strip()
        if "openrouter.ai" in self.embedding_base_url:
            return self.openrouter_api_key.strip()
        return ""

    def parsed_otel_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for pair in self.otel_exporter_otlp_headers.split(","):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            key = key.strip()
            if key:
                headers[key] = value.strip()
        return headers


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
