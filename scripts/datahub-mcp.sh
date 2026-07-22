#!/usr/bin/env bash
set -euo pipefail

readonly MCP_PACKAGE_SPEC="mcp-server-datahub==0.6.0"
readonly MCP_COMMAND="mcp-server-datahub"
readonly DEFAULT_ASSET_URN="urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)"
readonly SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

MCP_HOST="${DATAHUB_MCP_HOST:-127.0.0.1}"
MCP_PORT="${DATAHUB_MCP_PORT:-8001}"
GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:18080}"
UVX_BIN="${DATAHUB_MCP_UVX_BIN:-$(command -v uvx || true)}"
PYTHON_BIN="${DATAHUB_MCP_PYTHON_BIN:-$(command -v python3 || true)}"
RUNTIME_DIR="${DATAHUB_MCP_RUNTIME_DIR:-${TMPDIR:-/tmp}/datarescue-datahub-mcp}"
START_TIMEOUT_SECONDS="${DATAHUB_MCP_START_TIMEOUT_SECONDS:-180}"
MCP_ENDPOINT="http://${MCP_HOST}:${MCP_PORT}/mcp"
MCP_HEALTH_URL="http://${MCP_HOST}:${MCP_PORT}/health"
PID_FILE="${RUNTIME_DIR}/server-${MCP_PORT}.pid"
STATE_FILE="${RUNTIME_DIR}/server-${MCP_PORT}.state"
LOG_FILE="${RUNTIME_DIR}/server-${MCP_PORT}.log"
LOCK_DIR="${RUNTIME_DIR}/server-${MCP_PORT}.lock"
CONFIG_FINGERPRINT=""

usage() {
  cat <<'EOF'
Usage: scripts/datahub-mcp.sh <command> [arguments]

Commands:
  start                 Start the pinned MCP HTTP server in the background.
  stop                  Stop the server started by this script.
  ready                 Exit successfully only when the MCP health route is ready.
  status                Show managed-process and readiness state.
  run                   Run the pinned MCP HTTP server in the foreground.
  version               Print the pinned server's installed version.
  tools                 List tools exposed by the running MCP server.
  call <tool> [json]    Call one tool with a JSON object (default: {}).
  verify [asset-urn]    Verify tools/list and the three live context reads.
  endpoint              Print the local MCP endpoint.

Environment:
  DATAHUB_GMS_URL                 DataHub GMS URL (default http://127.0.0.1:18080)
  DATAHUB_GMS_TOKEN               Optional official DataHub SDK token
  DATARESCUE_DATAHUB_TOKEN        Fallback token when DATAHUB_GMS_TOKEN is unset
  DATAHUB_MCP_HOST                Loopback bind host (default 127.0.0.1)
  DATAHUB_MCP_PORT                Bind port (default 8001)
  DATAHUB_MCP_RUNTIME_DIR         PID/log directory outside the repository
  DATAHUB_MCP_ASSET_URN           Default asset for verify
EOF
}

die() {
  printf 'DataHub MCP: %s\n' "$1" >&2
  exit 1
}

validate_runtime_settings() {
  case "$MCP_HOST" in
    127.0.0.1 | localhost) ;;
    *) die "DATAHUB_MCP_HOST must remain on loopback" ;;
  esac
  [[ "$MCP_PORT" =~ ^[0-9]+$ ]] || die "DATAHUB_MCP_PORT must be an integer"
  ((MCP_PORT >= 1024 && MCP_PORT <= 65535)) || die "DATAHUB_MCP_PORT is out of range"
  [[ "$START_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || {
    die "DATAHUB_MCP_START_TIMEOUT_SECONDS must be an integer"
  }
  ((START_TIMEOUT_SECONDS > 0 && START_TIMEOUT_SECONDS <= 600)) || {
    die "DATAHUB_MCP_START_TIMEOUT_SECONDS must be between 1 and 600"
  }
  [[ "$GMS_URL" == http://* || "$GMS_URL" == https://* ]] || {
    die "DATAHUB_GMS_URL must use http or https"
  }
  [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || die "python3 is required"
  CONFIG_FINGERPRINT="$(config_fingerprint)"
}

require_uvx() {
  [[ -n "$UVX_BIN" && -x "$UVX_BIN" ]] || die "uvx is required"
}

configure_official_environment() {
  export DATAHUB_GMS_URL="$GMS_URL"
  if [[ -z "${DATAHUB_GMS_TOKEN:-}" && -n "${DATARESCUE_DATAHUB_TOKEN:-}" ]]; then
    export DATAHUB_GMS_TOKEN="$DATARESCUE_DATAHUB_TOKEN"
  fi
  export TOOLS_IS_MUTATION_ENABLED=true
  export SAVE_DOCUMENT_TOOL_ENABLED=true
  export DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED=false
  export FASTMCP_HOST="$MCP_HOST"
  export FASTMCP_PORT="$MCP_PORT"
  export FASTMCP_STREAMABLE_HTTP_PATH=/mcp
}

gms_ready() {
  configure_official_environment
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import os
import urllib.request

base_url = os.environ["DATAHUB_GMS_URL"].rstrip("/")
request = urllib.request.Request(f"{base_url}/config")
token = os.environ.get("DATAHUB_GMS_TOKEN")
if token:
    request.add_header("Authorization", f"Bearer {token}")
with urllib.request.urlopen(request, timeout=5) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
}

health_ready() {
  MCP_HEALTH_URL="$MCP_HEALTH_URL" "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import json
import os
import urllib.request

with urllib.request.urlopen(os.environ["MCP_HEALTH_URL"], timeout=2) as response:
    body = json.load(response)
if response.status != 200 or body != {"status": "ok"}:
    raise SystemExit(1)
PY
}

port_in_use() {
  MCP_HOST="$MCP_HOST" MCP_PORT="$MCP_PORT" "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import os
import socket

try:
    with socket.create_connection(
        (os.environ["MCP_HOST"], int(os.environ["MCP_PORT"])), timeout=1
    ):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
}

config_fingerprint() {
  FINGERPRINT_PACKAGE_SPEC="$MCP_PACKAGE_SPEC" FINGERPRINT_GMS_URL="$GMS_URL" \
    FINGERPRINT_MCP_HOST="$MCP_HOST" FINGERPRINT_MCP_PORT="$MCP_PORT" \
    FINGERPRINT_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-${DATARESCUE_DATAHUB_TOKEN:-}}" \
    "$PYTHON_BIN" - <<'PY'
import hashlib
import os

values = (
    os.environ["FINGERPRINT_PACKAGE_SPEC"],
    os.environ["FINGERPRINT_GMS_URL"].rstrip("/"),
    os.environ["FINGERPRINT_MCP_HOST"],
    os.environ["FINGERPRINT_MCP_PORT"],
    hashlib.sha256(os.environ["FINGERPRINT_GMS_TOKEN"].encode()).hexdigest(),
)
print(hashlib.sha256("\0".join(values).encode()).hexdigest())
PY
}

state_value() {
  local key="$1"
  [[ -f "$STATE_FILE" ]] || return 1
  awk -F= -v expected="$key" '$1 == expected { print substr($0, index($0, "=") + 1); exit }' \
    "$STATE_FILE"
}

write_state() {
  local pid="$1"
  local pgid="$2"
  local temporary="${STATE_FILE}.tmp.$$"
  {
    printf 'pid=%s\n' "$pid"
    printf 'pgid=%s\n' "$pgid"
    printf 'fingerprint=%s\n' "$CONFIG_FINGERPRINT"
  } >"$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$STATE_FILE"
  printf '%s\n' "$pid" >"$PID_FILE"
  chmod 600 "$PID_FILE"
}

clear_state() {
  rm -f "$PID_FILE" "$STATE_FILE"
}

state_pgid() {
  local pgid
  pgid="$(state_value pgid || true)"
  [[ "$pgid" =~ ^[0-9]+$ ]] && ((pgid > 1)) || return 1
  printf '%s\n' "$pgid"
}

group_pids() {
  local pgid="$1"
  ps -axo pid=,pgid=,stat= | awk -v expected="$pgid" \
    '$2 == expected && $3 !~ /^Z/ { print $1 }'
}

managed_group_alive() {
  local pgid="$1"
  [[ -n "$(group_pids "$pgid")" ]]
}

owned_server_group() {
  local pgid="$1"
  local pid command_line found="false"
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$command_line" == *"mcp-server-datahub"* ]]; then
      found="true"
      break
    fi
  done < <(group_pids "$pgid")
  [[ "$found" == "true" ]]
}

state_matches_configuration() {
  local stored
  stored="$(state_value fingerprint || true)"
  [[ "$stored" =~ ^[0-9a-f]{64}$ && "$stored" == "$CONFIG_FINGERPRINT" ]]
}

acquire_lock() {
  mkdir -p "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
  local attempt owner
  for attempt in $(seq 1 100); do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" >"${LOCK_DIR}/owner.pid"
      chmod 600 "${LOCK_DIR}/owner.pid"
      return 0
    fi
    owner="$(tr -d '[:space:]' <"${LOCK_DIR}/owner.pid" 2>/dev/null || true)"
    if [[ ! "$owner" =~ ^[0-9]+$ ]] || ! kill -0 "$owner" 2>/dev/null; then
      rm -f "${LOCK_DIR}/owner.pid"
      rmdir "$LOCK_DIR" 2>/dev/null || true
      continue
    fi
    sleep 0.1
  done
  die "timed out waiting for launcher lock ${LOCK_DIR}"
}

release_lock() {
  rm -f "${LOCK_DIR}/owner.pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

run_server() {
  configure_official_environment
  exec "$UVX_BIN" --from "$MCP_PACKAGE_SPEC" "$MCP_COMMAND" --transport http
}

start_server() {
  mkdir -p "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"

  local pid pgid
  if pgid="$(state_pgid)" && managed_group_alive "$pgid"; then
    owned_server_group "$pgid" || {
      die "managed process group ${pgid} does not belong to DataHub MCP"
    }
    state_matches_configuration || {
      die "managed server configuration differs from this launch request; stop it first"
    }
    if health_ready; then
      pid="$(state_value pid || true)"
      printf 'DataHub MCP already ready at %s (pid %s, pgid %s)\n' \
        "$MCP_ENDPOINT" "${pid:-unknown}" "$pgid"
      return 0
    fi
    die "managed process group exists but is not ready; inspect ${LOG_FILE}"
  elif [[ -f "$STATE_FILE" || -f "$PID_FILE" ]]; then
    clear_state
  fi
  if port_in_use; then
    die "port ${MCP_PORT} is already occupied by an unmanaged process"
  fi
  gms_ready || die "configured DataHub GMS is not ready"

  # A dedicated session makes the entire uvx -> Python server tree recoverable.
  # The group id is the recorded leader pid even when uvx later dies and leaves
  # its mutation-capable child behind.
  nohup "$PYTHON_BIN" -c \
    'import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
    "$SCRIPT_PATH" run >"$LOG_FILE" 2>&1 </dev/null &
  pid=$!
  pgid="$pid"
  write_state "$pid" "$pgid"
  chmod 600 "$LOG_FILE"

  # The background process needs a brief scheduling window to call setsid().
  # Until then it is alive but still belongs to the launcher's process group,
  # so treating a missing target pgid as an exit would orphan the new server.
  local session_deadline=$((SECONDS + 5))
  while ! managed_group_alive "$pgid"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      clear_state
      die "server exited during startup; inspect ${LOG_FILE}"
    fi
    if ((SECONDS >= session_deadline)); then
      kill -TERM "$pid" 2>/dev/null || true
      clear_state
      die "server did not establish its managed process group"
    fi
    sleep 0.05
  done

  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    if ! managed_group_alive "$pgid"; then
      clear_state
      die "server exited during startup; inspect ${LOG_FILE}"
    fi
    if health_ready; then
      owned_server_group "$pgid" || {
        terminate_group "$pgid"
        clear_state
        die "started process group does not contain the pinned DataHub MCP server"
      }
      printf 'DataHub MCP ready at %s (pid %s, pgid %s)\n' \
        "$MCP_ENDPOINT" "$pid" "$pgid"
      return 0
    fi
    sleep 0.5
  done

  if owned_server_group "$pgid"; then
    terminate_group "$pgid"
  fi
  clear_state
  die "server did not become ready within ${START_TIMEOUT_SECONDS}s"
}

terminate_group() {
  local pgid="$1"
  local shell_pgid
  shell_pgid="$(ps -p $$ -o pgid= | tr -d '[:space:]')"
  [[ "$pgid" != "$shell_pgid" ]] || die "refusing to signal the launcher's process group"
  kill -TERM -- "-${pgid}" 2>/dev/null || true
  local deadline=$((SECONDS + 10))
  while managed_group_alive "$pgid" && ((SECONDS < deadline)); do
    sleep 0.2
  done
  if managed_group_alive "$pgid"; then
    owned_server_group "$pgid" || {
      die "process-group identity changed while stopping pgid ${pgid}"
    }
    kill -KILL -- "-${pgid}" 2>/dev/null || true
  fi
}

stop_server() {
  local pid pgid
  if ! pgid="$(state_pgid)"; then
    clear_state
    printf 'DataHub MCP is not running under this launcher\n'
    return 0
  fi
  pid="$(state_value pid || true)"
  if ! managed_group_alive "$pgid"; then
    clear_state
    printf 'DataHub MCP is not running under this launcher\n'
    return 0
  fi
  owned_server_group "$pgid" || {
    die "refusing to stop pgid ${pgid}; process identity does not match"
  }

  terminate_group "$pgid"
  clear_state
  printf 'DataHub MCP stopped (pid %s, pgid %s)\n' "${pid:-unknown}" "$pgid"
}

rpc_client() {
  local operation="$1"
  shift
  MCP_ENDPOINT="$MCP_ENDPOINT" "$PYTHON_BIN" - "$operation" "$@" <<'PY'
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

endpoint = os.environ["MCP_ENDPOINT"]
operation = sys.argv[1]
MCP_PROTOCOL_VERSION = "2025-06-18"
session_id: str | None = None
protocol_version: str | None = None
next_id = 1


def sse_data_frames(text: str) -> list[str]:
    frames: list[str] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            frames.append("\n".join(data_lines))
            data_lines.clear()

    for raw_line in text.splitlines():
        if raw_line == "":
            flush()
            continue
        if raw_line.startswith(":") or not raw_line.startswith("data:"):
            continue
        value = raw_line[5:]
        if value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    flush()
    return frames


def validated_rpc_response(
    value: object, *, request_id: int | str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("MCP response was not an object")
    if value.get("jsonrpc") != "2.0":
        raise RuntimeError("MCP response has an invalid JSON-RPC version")
    if value.get("id") != request_id:
        raise RuntimeError(
            f"MCP response id {value.get('id')!r} does not match "
            f"request id {request_id!r}"
        )
    if "result" not in value and "error" not in value:
        raise RuntimeError("MCP response has neither result nor error")
    return value


def parse_body(
    raw: bytes, content_type: str, *, request_id: int | str
) -> dict[str, Any]:
    text = raw.decode("utf-8")
    if "text/event-stream" not in content_type.casefold():
        return validated_rpc_response(json.loads(text), request_id=request_id)
    for frame in sse_data_frames(text):
        value = json.loads(frame)
        if not isinstance(value, dict) or value.get("id") != request_id:
            continue
        return validated_rpc_response(value, request_id=request_id)
    raise RuntimeError(
        f"MCP event stream contained no response for request id {request_id!r}"
    )


def post(
    message: dict[str, Any],
    *,
    expect_body: bool = True,
    request_id: int | str | None = None,
) -> dict[str, Any] | None:
    global session_id, protocol_version
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["MCP-Session-Id"] = session_id
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(message).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            session_id = response.headers.get("MCP-Session-Id") or session_id
            raw = response.read()
            if not expect_body or not raw:
                return None
            if request_id is None:
                raise RuntimeError("MCP response cannot be matched without a request id")
            return parse_body(
                raw,
                response.headers.get("Content-Type", ""),
                request_id=request_id,
            )
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"MCP request failed with HTTP {error.code}") from error


def request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global next_id
    request_id = next_id
    next_id += 1
    body = post(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
        request_id=request_id,
    )
    if not isinstance(body, dict):
        raise RuntimeError("MCP request returned no response")
    if body.get("error"):
        raise RuntimeError(f"MCP {method} returned an error")
    result = body.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"MCP {method} returned no result object")
    return result


def tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        raise RuntimeError("MCP tool reported an error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        meta = result.get("_meta")
        fastmcp = meta.get("fastmcp") if isinstance(meta, dict) else None
        if (isinstance(fastmcp, dict) and fastmcp.get("wrap_result") is True) or set(
            structured
        ) == {"result"}:
            structured = structured.get("result")
        if isinstance(structured, dict):
            return structured
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                value = json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise RuntimeError("MCP tool returned no structured object")


initialize = request(
    "initialize",
    {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "datarescue-runtime-check", "version": "0.1.0"},
    },
)
negotiated_protocol = initialize.get("protocolVersion")
if negotiated_protocol != MCP_PROTOCOL_VERSION:
    raise RuntimeError(
        f"MCP negotiated unsupported protocol version {negotiated_protocol!r}; "
        f"expected {MCP_PROTOCOL_VERSION}"
    )
capabilities = initialize.get("capabilities")
if not isinstance(capabilities, dict):
    raise RuntimeError("MCP initialize did not return capabilities")
server_info = initialize.get("serverInfo")
if not isinstance(server_info, dict) or not isinstance(server_info.get("name"), str):
    raise RuntimeError("MCP initialize did not return valid serverInfo.name")
if not server_info["name"].strip():
    raise RuntimeError("MCP initialize returned an empty serverInfo.name")
protocol_version = negotiated_protocol
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_body=False)


def list_tools() -> list[str]:
    result = request("tools/list")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("MCP tools/list returned no tools array")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if not all(isinstance(name, str) and name for name in names):
        raise RuntimeError("MCP tools/list returned an invalid tool")
    return names


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return request("tools/call", {"name": name, "arguments": arguments})


if operation == "tools":
    print("\n".join(sorted(list_tools())))
elif operation == "call":
    if len(sys.argv) != 4:
        raise RuntimeError("call requires a tool name and JSON object")
    arguments = json.loads(sys.argv[3])
    if not isinstance(arguments, dict):
        raise RuntimeError("tool arguments must be a JSON object")
    result = call_tool(sys.argv[2], arguments)
    if result.get("isError"):
        raise RuntimeError("MCP tool reported an error")
    print(json.dumps(result, indent=2, sort_keys=True))
elif operation == "verify":
    asset_urn = sys.argv[2]
    required = {
        "get_entities",
        "grep_documents",
        "list_schema_fields",
        "get_lineage",
        "save_document",
    }
    names = set(list_tools())
    missing = sorted(required - names)
    if missing:
        raise RuntimeError(f"required MCP tools are missing: {', '.join(missing)}")
    entity = tool_payload(call_tool("get_entities", {"urns": asset_urn}))
    if entity.get("urn") != asset_urn:
        raise RuntimeError("get_entities did not return the requested asset")
    schema = tool_payload(
        call_tool("list_schema_fields", {"urn": asset_urn, "limit": 100, "offset": 0})
    )
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise RuntimeError("list_schema_fields returned no fields")
    lineage = tool_payload(
        call_tool(
            "get_lineage",
            {
                "urn": asset_urn,
                "upstream": False,
                "max_hops": 4,
                "max_results": 100,
                "offset": 0,
            },
        )
    )
    downstreams = lineage.get("downstreams")
    if not isinstance(downstreams, dict) or not isinstance(
        downstreams.get("searchResults"), list
    ):
        raise RuntimeError("get_lineage returned no downstream result array")
    print(f"tools/list=ok required={len(required)}")
    print("get_entities=ok")
    print(f"list_schema_fields=ok fields={len(fields)}")
    print(f"get_lineage=ok downstreams={len(downstreams['searchResults'])}")
else:
    raise RuntimeError(f"unknown RPC operation: {operation}")
PY
}

action="${1:-help}"
if (($#)); then
  shift
fi

case "$action" in
  help | -h | --help)
    usage
    ;;
  version)
    validate_runtime_settings
    require_uvx
    configure_official_environment
    "$UVX_BIN" --from "$MCP_PACKAGE_SPEC" "$MCP_COMMAND" --version
    ;;
  endpoint)
    validate_runtime_settings
    printf '%s\n' "$MCP_ENDPOINT"
    ;;
  start)
    validate_runtime_settings
    require_uvx
    acquire_lock
    trap release_lock EXIT
    trap 'exit 130' INT TERM
    start_server
    ;;
  stop)
    validate_runtime_settings
    acquire_lock
    trap release_lock EXIT
    trap 'exit 130' INT TERM
    stop_server
    ;;
  ready)
    validate_runtime_settings
    health_ready || die "server is not ready"
    printf 'DataHub MCP ready at %s\n' "$MCP_ENDPOINT"
    ;;
  status)
    validate_runtime_settings
    pgid="$(state_pgid || true)"
    pid="$(state_value pid || true)"
    if [[ -n "$pgid" ]] && managed_group_alive "$pgid" && ! owned_server_group "$pgid"; then
      die "managed pgid ${pgid} does not belong to the pinned DataHub MCP server"
    elif [[ -n "$pgid" ]] && managed_group_alive "$pgid" && ! state_matches_configuration; then
      die "managed server configuration differs from this status request"
    elif [[ -n "$pgid" ]] && managed_group_alive "$pgid" && health_ready; then
      printf 'running pid=%s pgid=%s endpoint=%s\n' "${pid:-unknown}" "$pgid" "$MCP_ENDPOINT"
    elif [[ -n "$pgid" ]] && managed_group_alive "$pgid"; then
      printf 'starting-or-unhealthy pid=%s pgid=%s endpoint=%s\n' \
        "${pid:-unknown}" "$pgid" "$MCP_ENDPOINT"
      exit 1
    else
      printf 'stopped endpoint=%s\n' "$MCP_ENDPOINT"
      exit 1
    fi
    ;;
  run)
    validate_runtime_settings
    require_uvx
    gms_ready || die "configured DataHub GMS is not ready"
    run_server
    ;;
  tools)
    validate_runtime_settings
    health_ready || die "server is not ready"
    rpc_client tools
    ;;
  call)
    validate_runtime_settings
    (($# >= 1 && $# <= 2)) || die "call requires <tool> and optional JSON object"
    health_ready || die "server is not ready"
    arguments_json="${2:-}"
    if [[ -z "$arguments_json" ]]; then
      arguments_json='{}'
    fi
    rpc_client call "$1" "$arguments_json"
    ;;
  verify)
    validate_runtime_settings
    (($# <= 1)) || die "verify accepts at most one asset URN"
    health_ready || die "server is not ready"
    rpc_client verify "${1:-${DATAHUB_MCP_ASSET_URN:-$DEFAULT_ASSET_URN}}"
    ;;
  *)
    usage >&2
    die "unknown command: ${action}"
    ;;
esac
