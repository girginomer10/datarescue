#!/usr/bin/env bash

# Shared fail-closed identity checks for launchers that own a local API process.
# The response headers are injected by uvicorn, so no application route needs to
# trust caller-provided identity data.

DATARESCUE_API_HOST_RESULT=""
DATARESCUE_API_PORT_RESULT=""
DATARESCUE_API_RUN_NONCE_RESULT=""
DATARESCUE_API_STATE_IDENTITY_RESULT=""

datarescue_configure_api_identity() {
  local api_url="${1:?API URL is required}"
  local database_path="${2:?database path is required}"
  local parsed=""
  parsed="$(DATARESCUE_API_URL_TO_PARSE="${api_url}" python3 - <<'PY'
import os
from urllib.parse import urlsplit

value = urlsplit(os.environ["DATARESCUE_API_URL_TO_PARSE"])
if value.scheme != "http" or value.hostname not in {"127.0.0.1", "localhost"}:
    raise SystemExit("DATARESCUE_API_URL must be a loopback http URL")
if value.username or value.password or value.query or value.fragment:
    raise SystemExit("DATARESCUE_API_URL must not contain credentials, query, or fragment")
if value.path not in {"", "/"}:
    raise SystemExit("DATARESCUE_API_URL must not contain an application path")
try:
    port = value.port
except ValueError as error:
    raise SystemExit(f"DATARESCUE_API_URL has an invalid port: {error}") from error
if port is None or not 1024 <= port <= 65535:
    raise SystemExit("DATARESCUE_API_URL must specify a port between 1024 and 65535")
host = "127.0.0.1" if value.hostname == "localhost" else value.hostname
print(f"{host}\t{port}")
PY
)"
  IFS=$'\t' read -r DATARESCUE_API_HOST_RESULT DATARESCUE_API_PORT_RESULT <<<"${parsed}"
  DATARESCUE_API_RUN_NONCE_RESULT="$(
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  )"
  DATARESCUE_API_STATE_IDENTITY_RESULT="$(
    DATARESCUE_IDENTITY_DATABASE_PATH="${database_path}" python3 - <<'PY'
import hashlib
import os
from pathlib import Path

database = str(Path(os.environ["DATARESCUE_IDENTITY_DATABASE_PATH"]).resolve())
print(hashlib.sha256(database.encode("utf-8")).hexdigest())
PY
  )"
}

datarescue_assert_api_port_available() {
  local host="${1:?API host is required}"
  local port="${2:?API port is required}"
  API_HOST="${host}" API_PORT="${port}" python3 - <<'PY'
import os
import socket

host = os.environ["API_HOST"]
port = int(os.environ["API_PORT"])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError as error:
    raise SystemExit(f"DataRescue API port {host}:{port} is already occupied: {error}") from error
finally:
    sock.close()
PY
}

datarescue_api_identity_ready() {
  local api_url="${1:?API URL is required}"
  local run_nonce="${2:?run nonce is required}"
  local state_identity="${3:?state identity is required}"
  API_URL="${api_url%/}" \
    EXPECTED_RUN_NONCE="${run_nonce}" \
    EXPECTED_STATE_IDENTITY="${state_identity}" python3 - <<'PY'
import json
import os
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen(f'{os.environ["API_URL"]}/health', timeout=2) as response:
        health = json.load(response)
        nonce = response.headers.get("X-DataRescue-Run-Nonce")
        state_identity = response.headers.get("X-DataRescue-State-Identity")
except (OSError, ValueError, urllib.error.URLError):
    raise SystemExit(1)
if health != {"status": "ok", "mode": "postgres"}:
    raise SystemExit(1)
if nonce != os.environ["EXPECTED_RUN_NONCE"]:
    raise SystemExit(1)
if state_identity != os.environ["EXPECTED_STATE_IDENTITY"]:
    raise SystemExit(1)
PY
}
