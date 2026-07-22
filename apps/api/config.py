from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CANONICAL_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)"
)


def _runtime_dir() -> Path:
    return Path(tempfile.gettempdir()) / "datarescue"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATARESCUE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    replay_mode: bool = True
    execution_mode: str = "replay"
    runtime_dir: Path = Field(default_factory=_runtime_dir)
    database_path: Path | None = None

    postgres_dsn: str | None = None
    dbt_project_dir: Path = Path("demo/dbt")
    dbt_profiles_dir: Path = Path("demo/dbt")
    dbt_target: str = "dev"
    allowed_assets: str = CANONICAL_ASSET_URN

    datahub_gms_url: str | None = None
    datahub_token: str | None = None
    datahub_mcp_url: str | None = None
    datahub_mcp_context_tool: str = "get_asset_context"
    datahub_mcp_write_tool: str = "write_document"

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-terra"
    openai_base_url: str = "https://api.openai.com/v1"

    github_write_enabled: bool = False
    github_repository: str = "girginomer10/datarescue"
    github_repo_root: Path = Path(".")
    github_base_branch: str = "main"
    github_patch_path: str = "demo/dbt/models/staging/stg_payments.sql"

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    web_dist_dir: Path = Path("apps/web/dist")

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path or self.runtime_dir / "state.sqlite3"

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allowed_asset_urns(self) -> set[str]:
        # DataHub dataset URNs contain commas, so semicolon/newline is the safe
        # delimiter for multiple allowlisted assets.
        normalized = self.allowed_assets.replace("\n", ";")
        return {asset.strip() for asset in normalized.split(";") if asset.strip()}
