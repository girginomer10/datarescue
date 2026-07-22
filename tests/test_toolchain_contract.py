from __future__ import annotations

from pathlib import Path

from apps.api.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_and_runtime_share_the_python_311_toolchain() -> None:
    """A fresh bootstrap must not let dbt replace a differently-versioned venv."""

    assert (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.11"
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "PYTHON_VERSION ?= 3.11" in makefile
    assert "UV_RUN := $(UV) run --python $(PYTHON_VERSION)" in makefile
    assert "$(UV) sync --python $(PYTHON_VERSION) --all-extras --locked" in makefile
    assert "DBT := $(UV_RUN)" in makefile
    assert "DATARESCUE_POSTGRES_DSN=$(DEMO_POSTGRES_DSN) $(UV_RUN) python" in makefile


def test_example_environment_documents_the_configurable_surface() -> None:
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    expected_backend = {
        f"DATARESCUE_{field.upper()}" for field in Settings.model_fields
    }
    missing_backend = sorted(key for key in expected_backend if key not in example)

    assert missing_backend == []
    for frontend_key in {
        "VITE_API_BASE_URL",
        "VITE_API_TIMEOUT_MS",
        "VITE_FORCE_REPLAY",
        "VITE_BASE_PATH",
        "VITE_API_PROXY_TARGET",
    }:
        assert frontend_key in example
