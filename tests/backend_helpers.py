from __future__ import annotations

from pathlib import Path

from apps.api.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_test_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": tmp_path / "state.sqlite3",
        "runtime_dir": tmp_path / "runtime",
        "github_repo_root": REPO_ROOT,
        "replay_mode": True,
        "execution_mode": "replay",
        "github_write_enabled": False,
        "datahub_gms_url": None,
        "datahub_mcp_url": None,
        "openai_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)
