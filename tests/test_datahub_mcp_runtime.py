from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "datahub-mcp.sh"


class _CapturedRPCServer(ThreadingHTTPServer):
    captured_requests: list[tuple[dict[str, Any], dict[str, str]]]


class _RuntimeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/config":
            body = b'{"versions":{"acryldata/datahub":{"version":"v1.6.0"}}}'
        elif self.path == "/health":
            body = b'{"status":"ok"}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _runtime_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RuntimeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _rpc_server(
    *, protocol_version: str = "2025-06-18", mismatched_tool_id: bool = False
) -> Iterator[_CapturedRPCServer]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            self._send(b'{"status":"ok"}', content_type="application/json")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            assert isinstance(self.server, _CapturedRPCServer)
            self.server.captured_requests.append(
                (body, {name.lower(): value for name, value in self.headers.items()})
            )
            method = body.get("method")
            if method == "initialize":
                payload = {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": protocol_version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "runtime-test", "version": "0.6.0"},
                    },
                }
                self._send(
                    json.dumps(payload).encode(),
                    content_type="application/json",
                    headers={"MCP-Session-Id": "runtime-session"},
                )
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "tools/list":
                response_id = 999 if mismatched_tool_id else body["id"]
                encoded_payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": response_id,
                        "result": {
                            "tools": [
                                {"name": "get_entities"},
                                {"name": "save_document"},
                            ]
                        },
                    },
                    separators=(",", ":"),
                )
                split_at = encoded_payload.index(",") + 1
                stream = (
                    'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                    f"data: {encoded_payload[:split_at]}\n"
                    f"data: {encoded_payload[split_at:]}\n\n"
                ).encode()
                self._send(stream, content_type="text/event-stream")
                return
            self.send_error(400)

        def _send(
            self,
            body: bytes,
            *,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _CapturedRPCServer(("127.0.0.1", 0), Handler)
    server.captured_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _fake_uvx(tmp_path: Path) -> Path:
    executable = tmp_path / "uvx"
    executable.write_text(
        """#!/bin/sh
printf 'args=%s\\n' "$*"
printf 'gms=%s\\n' "${DATAHUB_GMS_URL:-}"
printf 'mutation=%s\\n' "${TOOLS_IS_MUTATION_ENABLED:-}"
printf 'save_document=%s\\n' "${SAVE_DOCUMENT_TOOL_ENABLED:-}"
printf 'host=%s\\n' "${FASTMCP_HOST:-}"
printf 'port=%s\\n' "${FASTMCP_PORT:-}"
if [ "${DATAHUB_GMS_TOKEN:-}" = "test-secret" ]; then
  printf 'token_present=yes\\n'
fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _fake_supervising_uvx(tmp_path: Path) -> Path:
    server = tmp_path / "mcp-server-datahub-fake.py"
    server.write_text(
        """from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


ThreadingHTTPServer(
    (os.environ["FASTMCP_HOST"], int(os.environ["FASTMCP_PORT"])), Handler
).serve_forever()
""",
        encoding="utf-8",
    )
    executable = tmp_path / "uvx-supervisor"
    executable.write_text(
        f"""#!/bin/sh
"${{DATAHUB_MCP_PYTHON_BIN:-{sys.executable}}}" "{server}" &
child=$!
trap 'kill -TERM "$child" 2>/dev/null || true' TERM INT
wait "$child"
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _health_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
            return bool(response.status == 200 and response.read() == b'{"status": "ok"}')
    except (OSError, urllib.error.URLError):
        return False


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _managed_env(
    tmp_path: Path,
    *,
    gms_port: int,
    mcp_port: int,
    token: str = "launcher-token",
) -> dict[str, str]:
    return {
        "DATAHUB_MCP_UVX_BIN": str(_fake_supervising_uvx(tmp_path)),
        "DATAHUB_MCP_PYTHON_BIN": sys.executable,
        "DATAHUB_GMS_URL": f"http://127.0.0.1:{gms_port}",
        "DATAHUB_GMS_TOKEN": token,
        "DATAHUB_MCP_PORT": str(mcp_port),
        "DATAHUB_MCP_RUNTIME_DIR": str(tmp_path / "runtime"),
        "DATAHUB_MCP_START_TIMEOUT_SECONDS": "10",
    }


def _state_values(env: dict[str, str], port: int) -> dict[str, str]:
    state = Path(env["DATAHUB_MCP_RUNTIME_DIR"]) / f"server-{port}.state"
    return dict(line.split("=", 1) for line in state.read_text(encoding="utf-8").splitlines())


def _force_group_cleanup(pgid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _run(
    *arguments: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *arguments],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_version_uses_the_exact_official_package_pin(tmp_path: Path) -> None:
    result = _run(
        "version",
        env={
            "DATAHUB_MCP_UVX_BIN": str(_fake_uvx(tmp_path)),
            "DATAHUB_GMS_TOKEN": "test-secret",
        },
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == (
        "args=--from mcp-server-datahub==0.6.0 mcp-server-datahub --version"
    )
    assert "test-secret" not in result.stdout
    assert "test-secret" not in result.stderr


def test_foreground_run_wires_live_gms_loopback_and_mutation_tools(
    tmp_path: Path,
) -> None:
    with _runtime_server() as gms:
        gms_port = gms.server_address[1]
        result = _run(
            "run",
            env={
                "DATAHUB_MCP_UVX_BIN": str(_fake_uvx(tmp_path)),
                "DATAHUB_GMS_URL": f"http://127.0.0.1:{gms_port}",
                "DATAHUB_GMS_TOKEN": "test-secret",
                "DATAHUB_MCP_PORT": "18001",
            },
        )

    assert result.returncode == 0
    assert "args=--from mcp-server-datahub==0.6.0 mcp-server-datahub --transport http" in (
        result.stdout
    )
    assert f"gms=http://127.0.0.1:{gms_port}" in result.stdout
    assert "mutation=true" in result.stdout
    assert "save_document=true" in result.stdout
    assert "host=127.0.0.1" in result.stdout
    assert "port=18001" in result.stdout
    assert "token_present=yes" in result.stdout
    assert "test-secret" not in result.stdout
    assert "test-secret" not in result.stderr


def test_ready_requires_the_exact_health_payload(tmp_path: Path) -> None:
    with _runtime_server() as server:
        port = server.server_address[1]
        result = _run(
            "ready",
            env={
                "DATAHUB_MCP_UVX_BIN": str(_fake_uvx(tmp_path)),
                "DATAHUB_MCP_PORT": str(port),
            },
        )

    assert result.returncode == 0
    assert f"DataHub MCP ready at http://127.0.0.1:{port}/mcp" in result.stdout


def test_launcher_refuses_non_loopback_bind(tmp_path: Path) -> None:
    result = _run(
        "endpoint",
        env={
            "DATAHUB_MCP_UVX_BIN": str(_fake_uvx(tmp_path)),
            "DATAHUB_MCP_HOST": "0.0.0.0",
        },
    )

    assert result.returncode == 1
    assert "must remain on loopback" in result.stderr


def test_shell_rpc_matches_multiline_sse_by_id_and_sends_negotiated_protocol(
    tmp_path: Path,
) -> None:
    with _rpc_server() as server:
        port = int(server.server_address[1])
        result = _run(
            "tools",
            env={
                "DATAHUB_MCP_PYTHON_BIN": sys.executable,
                "DATAHUB_MCP_PORT": str(port),
                "DATAHUB_MCP_RUNTIME_DIR": str(tmp_path / "runtime"),
                "DATAHUB_GMS_TOKEN": "gms-secret",
                "DATARESCUE_DATAHUB_MCP_TOKEN": "mcp-secret",
            },
        )
        captured = server.captured_requests

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["get_entities", "save_document"]
    assert [body.get("id") for body, _headers in captured] == [1, None, 2]
    assert "mcp-protocol-version" not in captured[0][1]
    assert captured[1][1]["mcp-protocol-version"] == "2025-06-18"
    assert captured[2][1]["mcp-protocol-version"] == "2025-06-18"
    assert captured[2][1]["mcp-session-id"] == "runtime-session"
    assert all("authorization" not in headers for _body, headers in captured)
    assert "gms-secret" not in result.stdout + result.stderr
    assert "mcp-secret" not in result.stdout + result.stderr


def test_shell_rpc_rejects_unsupported_protocol_and_mismatched_sse_id(
    tmp_path: Path,
) -> None:
    with _rpc_server(protocol_version="2024-11-05") as old_server:
        old_protocol = _run(
            "tools",
            env={
                "DATAHUB_MCP_PYTHON_BIN": sys.executable,
                "DATAHUB_MCP_PORT": str(old_server.server_address[1]),
                "DATAHUB_MCP_RUNTIME_DIR": str(tmp_path / "old-runtime"),
            },
        )
    assert old_protocol.returncode != 0
    assert "unsupported protocol version" in old_protocol.stderr

    with _rpc_server(mismatched_tool_id=True) as wrong_id_server:
        wrong_id = _run(
            "tools",
            env={
                "DATAHUB_MCP_PYTHON_BIN": sys.executable,
                "DATAHUB_MCP_PORT": str(wrong_id_server.server_address[1]),
                "DATAHUB_MCP_RUNTIME_DIR": str(tmp_path / "wrong-id-runtime"),
            },
        )
    assert wrong_id.returncode != 0
    assert "no response for request id 2" in wrong_id.stderr


def test_stop_recovers_orphaned_server_child_after_launcher_leader_dies(
    tmp_path: Path,
) -> None:
    mcp_port = _free_port()
    with _runtime_server() as gms:
        env = _managed_env(
            tmp_path,
            gms_port=int(gms.server_address[1]),
            mcp_port=mcp_port,
        )
        started = _run("start", env=env)
        assert started.returncode == 0, started.stderr
        state = _state_values(env, mcp_port)
        pid = int(state["pid"])
        pgid = int(state["pgid"])
        assert pid == pgid
        assert _health_ready(mcp_port)

        try:
            os.kill(pid, signal.SIGKILL)
            assert _wait_until(lambda: _health_ready(mcp_port))

            stopped = _run("stop", env=env)
            assert stopped.returncode == 0, stopped.stderr
            assert f"pgid {pgid}" in stopped.stdout
            assert _wait_until(lambda: not _health_ready(mcp_port))
            assert not (Path(env["DATAHUB_MCP_RUNTIME_DIR"]) / f"server-{mcp_port}.state").exists()
        finally:
            _force_group_cleanup(pgid)


def test_start_rejects_reuse_when_effective_gms_token_changes(tmp_path: Path) -> None:
    mcp_port = _free_port()
    with _runtime_server() as gms:
        env = _managed_env(
            tmp_path,
            gms_port=int(gms.server_address[1]),
            mcp_port=mcp_port,
            token="first-token",
        )
        started = _run("start", env=env)
        assert started.returncode == 0, started.stderr
        state = _state_values(env, mcp_port)
        pgid = int(state["pgid"])

        try:
            state_text = (
                Path(env["DATAHUB_MCP_RUNTIME_DIR"]) / f"server-{mcp_port}.state"
            ).read_text(encoding="utf-8")
            assert "first-token" not in state_text

            changed = _run("start", env={**env, "DATAHUB_GMS_TOKEN": "second-token"})
            assert changed.returncode == 1
            assert "configuration differs" in changed.stderr
            assert "first-token" not in changed.stderr
            assert "second-token" not in changed.stderr
        finally:
            stopped = _run("stop", env=env)
            if stopped.returncode != 0:
                _force_group_cleanup(pgid)


def test_concurrent_starts_serialize_and_reuse_one_managed_server(tmp_path: Path) -> None:
    mcp_port = _free_port()
    with _runtime_server() as gms:
        env = _managed_env(
            tmp_path,
            gms_port=int(gms.server_address[1]),
            mcp_port=mcp_port,
        )
        command = [str(SCRIPT), "start"]
        process_env = {**os.environ, **env}
        first = subprocess.Popen(
            command,
            cwd=ROOT,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            command,
            cwd=ROOT,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)

        state = _state_values(env, mcp_port)
        pgid = int(state["pgid"])
        try:
            assert first.returncode == 0, first_stderr
            assert second.returncode == 0, second_stderr
            combined = first_stdout + second_stdout
            assert combined.count("DataHub MCP ready at") == 1
            assert "already ready" in combined
            assert _health_ready(mcp_port)
        finally:
            stopped = _run("stop", env=env)
            if stopped.returncode != 0:
                _force_group_cleanup(pgid)
