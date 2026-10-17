"""Typed configuration for the Open Agent Platform runtime.

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
    oap_default_model: str = "anthropic/claude-sonnet-5.5"
    oap_builder_model: str = "anthropic/claude-sonnet-5.5"
    oap_engineer_model: str = "anthropic/claude-sonnet-5.5"
    oap_manager_model: str = "anthropic/claude-haiku-5.5"
    oap_model_max_tokens: int = 16384

    # --- Embeddings ---------------------------------------------------------
    # Configured separately from chat. The default host is the OpenAI API.
    # Point EMBEDDING_BASE_URL at OpenRouter to reuse OPENROUTER_API_KEY.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"

    # --- OAP runtime -------------------------------------------------------
    oap_id: str = "oap"
    oap_port: int = 8000
    os_security_key: str = ""
    oap_telemetry: bool = False
    oap_debug: bool = False
    enable_mcp_server: bool = True

    # --- File watcher -------------------------------------------------------
    enable_file_watcher: bool = True
    watcher_debounce_seconds: float = 2.0

    # --- OpenTelemetry ------------------------------------------------------
    otel_enabled: bool = True
    otel_service_name: str = "oap-api"
    otel_environment: str = "development"
    # Base OTLP/HTTP collector URL. The `/v1/traces` suffix is appended by this
    # application, matching the OpenTelemetry specification for
    # OTEL_EXPORTER_OTLP_ENDPOINT.
    otel_exporter_otlp_endpoint: str = "http://oap-otel:6006"
    otel_exporter_otlp_headers: str = ""

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://ai:ai@oap-db:5432/oap"

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
        """Embedding key, falling back to OpenRouter only for OpenRouter URLs.

        An OpenRouter key is not valid at api.openai.com, so the fallback
        applies only when the embedding base URL is actually OpenRouter.
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
