"""Runtime configuration, loaded from environment or .env.

Everything the system needs to reach the outside world is resolved here, so no
module below this one reads os.environ directly. That keeps the source adapters
and agents testable without a live environment.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---- Regulations.gov ----
    regulations_api_key: str = Field(default="", alias="REGULATIONS_GOV_API_KEY")
    regulations_base_url: str = Field(
        default="https://api.regulations.gov/v4", alias="REGULATIONS_GOV_BASE_URL"
    )

    # ---- Google Cloud ----
    gcp_project: str = Field(default="", alias="GOOGLE_CLOUD_PROJECT")
    use_vertex: bool = Field(default=True, alias="GOOGLE_GENAI_USE_VERTEXAI")

    # Model serving and infrastructure live in DIFFERENT places, and conflating
    # them costs an afternoon.
    #
    # vertex_location: where Gemini is served from. Verified 2026-08-21 against
    #   this project: every Gemini 3.x model returns 404 NOT_FOUND from
    #   us-central1 and only responds on the global endpoint. Note that
    #   client.models.list() happily reports them under us-central1 anyway, so
    #   the listing is not evidence that a model will serve.
    # gcp_region: where Cloud Run, Firestore, and the bucket live. Regional.
    vertex_location: str = Field(default="global", alias="GOOGLE_CLOUD_LOCATION")
    gcp_region: str = Field(default="us-central1", alias="GCP_REGION")

    # ---- Models ----
    triage_model: str = Field(default="gemini-3.5-flash", alias="TRIAGE_MODEL")
    analyst_model: str = Field(default="gemini-3.5-pro", alias="ANALYST_MODEL")

    # ---- Storage ----
    firestore_database: str = Field(default="(default)", alias="FIRESTORE_DATABASE")
    gcs_bucket: str = Field(default="", alias="GCS_BUCKET")

    # ---- Behavior ----
    initial_lookback_days: int = Field(default=7, alias="INITIAL_LOOKBACK_DAYS")
    max_docs_per_run: int = Field(default=40, alias="MAX_DOCS_PER_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    def require_regulations_key(self) -> str:
        if not self.regulations_api_key or "paste-your" in self.regulations_api_key:
            raise RuntimeError(
                "REGULATIONS_GOV_API_KEY is not set. Copy .env.example to .env "
                "and paste the key you received from regulations.gov."
            )
        return self.regulations_api_key


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


def configure_genai_env() -> None:
    """Publish Vertex settings into os.environ for ADK and google-genai.

    pydantic-settings reads .env into a Settings object but deliberately does
    not touch os.environ. ADK constructs its own genai client and reads these
    variables directly, so without this bridge an agent silently falls back to
    the Gemini Developer API (and then fails on a missing GOOGLE_API_KEY)
    even though .env clearly says use Vertex.

    Existing environment variables win, so a shell override still works.
    """
    cfg = settings()
    for key, value in (
        ("GOOGLE_GENAI_USE_VERTEXAI", "TRUE" if cfg.use_vertex else "FALSE"),
        ("GOOGLE_CLOUD_PROJECT", cfg.gcp_project),
        # Must be the model-serving location, not the infra region.
        ("GOOGLE_CLOUD_LOCATION", cfg.vertex_location),
    ):
        if value and not os.environ.get(key):
            os.environ[key] = value
