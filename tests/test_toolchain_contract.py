from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def test_datahub_v16_runtime_uses_the_gms_schema_registry_proxy() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    runtime = (REPO_ROOT / "scripts" / "demo-runtime.sh").read_text(encoding="utf-8")

    assert "DATAHUB_MAPPED_GMS_PORT ?= 18080" in makefile
    assert "$(DATAHUB_GMS_URL)/schema-registry/api/" in makefile
    assert "DATAHUB_MAPPED_GMS_PORT=18080" in example
    assert "http://127.0.0.1:18080/schema-registry/api/" in example
    assert "host.docker.internal:${DATAHUB_MAPPED_GMS_PORT}" in runtime
    assert "127.0.0.1:8081" not in makefile
    assert "127.0.0.1:8081" not in example


def test_datahub_down_stops_the_same_pinned_release_started_by_up() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "docker quickstart --version $(DATAHUB_RELEASE) --dump-logs-on-failure" in makefile
    assert "docker quickstart --version $(DATAHUB_RELEASE) --stop" in makefile


def test_datahub_v16_recipes_preserve_the_canonical_dataset_urn() -> None:
    postgres = (REPO_ROOT / "demo" / "datahub" / "postgres-ingestion.yml").read_text(
        encoding="utf-8"
    )
    dbt = (REPO_ROOT / "demo" / "datahub" / "dbt-ingestion.yml").read_text(
        encoding="utf-8"
    )

    assert "include_table_lineage" not in postgres
    assert "platform_instance:" not in postgres
    assert "target_platform_instance:" not in dbt
    assert "load_schemas:" not in dbt
    assert "include_database_name: true" in dbt


def test_connected_launcher_manages_the_pinned_loopback_mcp_by_default() -> None:
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    launcher = (REPO_ROOT / "scripts" / "demo-connected.sh").read_text(encoding="utf-8")
    mcp_launcher = (REPO_ROOT / "scripts" / "datahub-mcp.sh").read_text(encoding="utf-8")

    assert "DATARESCUE_DATAHUB_MCP_URL=\n" in example
    assert 'if [[ -z "${DATARESCUE_DATAHUB_MCP_URL:-}" ]]' in launcher
    assert 'bash scripts/datahub-mcp.sh start' in launcher
    assert 'bash scripts/datahub-mcp.sh stop' in launcher
    assert 'MCP_PACKAGE_SPEC="mcp-server-datahub==0.6.0"' in mcp_launcher
    assert "TOOLS_IS_MUTATION_ENABLED=true" in mcp_launcher


def test_datahub_mcp_call_preserves_explicit_json_arguments() -> None:
    launcher = (REPO_ROOT / "scripts" / "datahub-mcp.sh").read_text(encoding="utf-8")

    assert 'arguments_json="${2:-}"' in launcher
    assert "arguments_json='{}'" in launcher
    assert 'rpc_client call "$1" "$arguments_json"' in launcher
    assert 'rpc_client call "$1" "${2:-{}}"' not in launcher


def test_connected_launchers_require_a_proof_owned_api_identity() -> None:
    connected = (REPO_ROOT / "scripts" / "demo-connected.sh").read_text(encoding="utf-8")
    mcl_proof = (REPO_ROOT / "scripts" / "datahub-mcl-proof.sh").read_text(
        encoding="utf-8"
    )

    for launcher in (connected, mcl_proof):
        assert 'source "${REPO_ROOT}/scripts/api-process-identity.sh"' in launcher
        assert 'assert_api_port_available' in launcher
        assert 'if ! kill -0 "${api_pid}"' in launcher
        assert 'X-DataRescue-Run-Nonce:${api_run_nonce}' in launcher
        assert 'X-DataRescue-State-Identity:${api_state_identity}' in launcher
        assert '--host "${api_host}" --port "${api_port}"' in launcher
        assert '"${DATARESCUE_API_URL}" "${api_run_nonce}" "${api_state_identity}"' in launcher
    assert 'DATARESCUE_API_URL="${DATARESCUE_API_URL:-http://127.0.0.1:8000}"' in connected
    assert 'DATARESCUE_API_URL="${DATARESCUE_API_URL:-http://127.0.0.1:8000}"' in mcl_proof
    assert '--port 8000' not in connected
    assert '--port 8000' not in mcl_proof


def test_api_identity_helper_does_not_collide_with_readonly_launcher_names(
    tmp_path: Path,
) -> None:
    helper = REPO_ROOT / "scripts" / "api-process-identity.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'set -euo pipefail; readonly API_DATABASE_PATH="$1"; source "$2"; '
                'datarescue_configure_api_identity "http://127.0.0.1:18091" '
                '"$API_DATABASE_PATH"; test "$DATARESCUE_API_PORT_RESULT" = 18091; '
                'test -n "$DATARESCUE_API_STATE_IDENTITY_RESULT"'
            ),
            "api-identity-test",
            str(tmp_path / "state.sqlite3"),
            str(helper),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_api_identity_helper_honors_override_port_and_rejects_occupied_port(
    tmp_path: Path,
) -> None:
    library = REPO_ROOT / "scripts" / "api-process-identity.sh"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    database = tmp_path / "owned-state.sqlite3"
    script = (
        'source "$1"; datarescue_configure_api_identity "$2" "$3"; '
        'datarescue_assert_api_port_available "$DATARESCUE_API_HOST_RESULT" '
        '"$DATARESCUE_API_PORT_RESULT"; '
        'printf "%s\\t%s\\t%s\\t%s\\n" "$DATARESCUE_API_HOST_RESULT" '
        '"$DATARESCUE_API_PORT_RESULT" "$DATARESCUE_API_RUN_NONCE_RESULT" '
        '"$DATARESCUE_API_STATE_IDENTITY_RESULT"'
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "_",
            str(library),
            f"http://localhost:{free_port}",
            str(database),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    host, raw_port, nonce, state_identity = result.stdout.strip().split("\t")
    assert host == "127.0.0.1"
    assert int(raw_port) == free_port
    assert len(nonce) == 48
    assert state_identity == hashlib.sha256(
        str(database.resolve()).encode("utf-8")
    ).hexdigest()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied_port = listener.getsockname()[1]
        occupied = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; datarescue_assert_api_port_available 127.0.0.1 "$2"',
                "_",
                str(library),
                str(occupied_port),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    assert occupied.returncode != 0
    assert "already occupied" in occupied.stderr


def test_api_identity_helper_requires_matching_nonce_and_state_headers() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"status":"ok","mode":"postgres"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-DataRescue-Run-Nonce", "owned-run")
            self.send_header("X-DataRescue-State-Identity", "owned-state")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        library = REPO_ROOT / "scripts" / "api-process-identity.sh"

        def check(nonce: str, state: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; datarescue_api_identity_ready "$2" "$3" "$4"',
                    "_",
                    str(library),
                    endpoint,
                    nonce,
                    state,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        assert check("owned-run", "owned-state").returncode == 0
        assert check("wrong-run", "owned-state").returncode != 0
        assert check("owned-run", "wrong-state").returncode != 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_connected_preflight_separates_mcp_auth_and_checks_semantic_contract() -> None:
    launcher = (REPO_ROOT / "scripts" / "demo-connected.sh").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert 'token=os.environ.get("DATARESCUE_DATAHUB_MCP_TOKEN") or None' in launcher
    assert 'export DATAHUB_TOKEN="${DATARESCUE_DATAHUB_TOKEN}"' in launcher
    assert 'export DATARESCUE_DATAHUB_TOKEN="${DATAHUB_TOKEN}"' in launcher
    assert 'context.get("owner") != "Finance Data"' in launcher
    assert 'CANONICAL_CONTEXT_DOCUMENT_URN not in documents' in launcher
    assert 'context.get("lineage_current") is not True' in launcher
    assert 'for token in ("net_amount", "gross_amount")' in launcher
    assert "DATAHUB_TOKEN := $(DATARESCUE_DATAHUB_TOKEN)" in makefile
    assert "DATARESCUE_DATAHUB_TOKEN := $(DATAHUB_TOKEN)" in makefile
    assert "export DATAHUB_TOKEN DATARESCUE_DATAHUB_TOKEN" in makefile

    mcl_proof = (REPO_ROOT / "scripts" / "datahub-mcl-proof.sh").read_text(
        encoding="utf-8"
    )
    assert 'export DATAHUB_TOKEN="${DATARESCUE_DATAHUB_TOKEN}"' in mcl_proof
    assert 'export DATARESCUE_DATAHUB_TOKEN="${DATAHUB_TOKEN}"' in mcl_proof


def test_dbt_docs_cannot_replace_the_build_run_results(tmp_path: Path) -> None:
    project = tmp_path / "dbt-project"
    target = project / "target"
    target.mkdir(parents=True)
    build_run_results = {
        "metadata": {"invocation_id": "successful-build"},
        "args": {"which": "build", "invocation_command": "dbt build"},
        "results": [{"unique_id": "model.datarescue.fct_revenue", "status": "success"}],
    }
    (target / "run_results.json").write_text(
        json.dumps(build_run_results), encoding="utf-8"
    )

    fake_dbt = tmp_path / "fake_dbt.py"
    fake_dbt.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            arguments = sys.argv[1:]
            assert arguments[:2] == ["docs", "generate"]
            project = Path(arguments[arguments.index("--project-dir") + 1])
            docs_target = Path(arguments[arguments.index("--target-path") + 1])
            assert docs_target != project / "target"
            docs_target.mkdir(parents=True, exist_ok=True)
            (docs_target / "manifest.json").write_text(
                json.dumps({"metadata": {"generated_by": "docs"}}), encoding="utf-8"
            )
            (docs_target / "catalog.json").write_text(
                json.dumps({"metadata": {"generated_by": "docs"}}), encoding="utf-8"
            )
            (docs_target / "run_results.json").write_text(
                json.dumps({"args": {"which": "generate"}}), encoding="utf-8"
            )
            """
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-o",
            "postgres-up",
            "dbt-docs",
            f"DBT={sys.executable} {fake_dbt}",
            f"DBT_PROJECT_DIR={project}",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    published_run_results = json.loads(
        (target / "run_results.json").read_text(encoding="utf-8")
    )
    assert published_run_results == build_run_results
    assert published_run_results["args"]["which"] == "build"
    assert json.loads((target / "manifest.json").read_text(encoding="utf-8")) == {
        "metadata": {"generated_by": "docs"}
    }
    assert json.loads((target / "catalog.json").read_text(encoding="utf-8")) == {
        "metadata": {"generated_by": "docs"}
    }


def test_selected_dbt_artifacts_are_built_in_isolation_then_published(
    tmp_path: Path,
) -> None:
    project = tmp_path / "dbt-project"
    project.mkdir()
    published = tmp_path / "immutable-snapshot"
    invocation_log = tmp_path / "dbt-invocations.jsonl"
    fake_dbt = tmp_path / "fake_dbt.py"
    fake_dbt.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path

            arguments = sys.argv[1:]
            operation = arguments[:2] if arguments[0] == "docs" else arguments[:1]
            target = Path(arguments[arguments.index("--target-path") + 1])
            target.mkdir(parents=True, exist_ok=True)
            with Path(os.environ["FAKE_DBT_LOG"]).open("a", encoding="utf-8") as log:
                log.write(
                    json.dumps(
                        {
                            "operation": operation,
                            "target": str(target),
                            "revenue_column": os.environ["DATARESCUE_REVENUE_COLUMN"],
                            "schema": os.environ["DBT_SCHEMA"],
                        }
                    )
                    + "\\n"
                )
            if arguments[0] == "build":
                (target / "run_results.json").write_text(
                    json.dumps({"args": {"which": "build"}, "results": []}),
                    encoding="utf-8",
                )
                # A build manifest must never be the one published for catalog ingestion.
                (target / "manifest.json").write_text(
                    json.dumps({"source": "build"}), encoding="utf-8"
                )
            elif arguments[:2] == ["docs", "generate"]:
                (target / "manifest.json").write_text(
                    json.dumps({"source": "docs"}), encoding="utf-8"
                )
                (target / "catalog.json").write_text(
                    json.dumps({"source": "docs"}), encoding="utf-8"
                )
                (target / "run_results.json").write_text(
                    json.dumps({"args": {"which": "generate"}}), encoding="utf-8"
                )
            else:
                raise AssertionError(arguments)
            """
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-o",
            "postgres-up",
            "dbt-artifacts-selected",
            f"DBT={sys.executable} {fake_dbt}",
            f"DBT_PROJECT_DIR={project}",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "FAKE_DBT_LOG": str(invocation_log),
            "DBT_ARTIFACT_OUTPUT_DIR": str(published),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    invocations = [
        json.loads(line) for line in invocation_log.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["operation"] for item in invocations] == [
        ["build"],
        ["docs", "generate"],
    ]
    targets = {item["target"] for item in invocations}
    assert len(targets) == 2
    assert str(project / "target") not in targets
    assert {item["revenue_column"] for item in invocations} == {"net_amount"}
    assert {item["schema"] for item in invocations} == {"analytics"}
    assert not (project / "target").exists()
    assert json.loads((published / "run_results.json").read_text(encoding="utf-8"))[
        "args"
    ]["which"] == "build"
    assert json.loads((published / "manifest.json").read_text(encoding="utf-8")) == {
        "source": "docs"
    }
    assert json.loads((published / "catalog.json").read_text(encoding="utf-8")) == {
        "source": "docs"
    }


def test_datahub_dbt_ingestion_consumes_a_per_run_snapshot_only() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    runtime = (REPO_ROOT / "scripts" / "demo-runtime.sh").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'snapshot="$$(mktemp -d "$(DBT_ARTIFACT_SNAPSHOT_ROOT)/dbt-ingest.XXXXXX")"' in makefile
    assert 'DBT_ARTIFACT_OUTPUT_DIR="$$snapshot"' in makefile
    assert 'DBT_ARTIFACT_ROOT_HOST="$$snapshot" $(DEMO_RUNTIME) datahub-ingest-dbt' in makefile
    assert 'for artifact in manifest.json catalog.json run_results.json' in runtime
    assert '--volume "${artifact_root}:/artifacts:ro"' in runtime
    assert '--volume "${repo_root}:/workspace:ro"' not in runtime
    assert '${DBT_ARTIFACT_ROOT_HOST:-./demo/dbt/target}:/artifacts:ro' in compose
    assert './:/workspace:ro' not in compose


def test_dbt_artifact_publisher_fails_closed_when_another_publisher_holds_lock(
    tmp_path: Path,
) -> None:
    project = tmp_path / "dbt-project"
    project.mkdir()
    lock_file = tmp_path / "publish.lock"
    ready = tmp_path / "holder-ready"
    helper = REPO_ROOT / "scripts" / "dbt-artifact-lock.py"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            "--lock",
            str(lock_file),
            "--ready",
            str(ready),
            "--owner-pid",
            str(os.getpid()),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)
        stderr = holder.stderr.read() if holder.stderr else ""
        raise AssertionError(f"lock holder failed to become ready: {stderr}")

    try:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-o",
                "postgres-up",
                "dbt-artifacts-selected",
                "DBT=false",
                f"DBT_PROJECT_DIR={project}",
                f"DBT_ARTIFACT_LOCK={lock_file}",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)

    assert result.returncode != 0
    assert "Unable to acquire the dbt artifact publication lock" in result.stderr
    assert not (project / "target").exists()

    # A SIGKILL leaves the regular lock file behind, but the kernel releases the
    # advisory lock. A new holder must recover without deleting or trusting a PID.
    replacement_ready = tmp_path / "replacement-ready"
    replacement = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            "--lock",
            str(lock_file),
            "--ready",
            str(replacement_ready),
            "--owner-pid",
            str(os.getpid()),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            not replacement_ready.exists()
            and replacement.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not replacement_ready.exists():
            if replacement.poll() is None:
                replacement.terminate()
                replacement.wait(timeout=5)
            stderr = replacement.stderr.read() if replacement.stderr else ""
            raise AssertionError(f"replacement lock failed to become ready: {stderr}")
    finally:
        if replacement.poll() is None:
            replacement.terminate()
            replacement.wait(timeout=5)
