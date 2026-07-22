from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field

from apps.api.models import IntegrationResult, IntegrationStatus


class MCPContextResult(BaseModel):
    integration: IntegrationResult
    context: dict[str, Any] = Field(default_factory=dict)


class DataHubMCPAdapter:
    def __init__(
        self,
        *,
        endpoint: str | None,
        token: str | None = None,
        context_tool: str = "get_asset_context",
        write_tool: str = "write_document",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.context_tool = context_tool
        self.write_tool = write_tool
        self.transport = transport

    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        if not self.endpoint:
            return MCPContextResult(
                integration=self._not_configured("mcp_fetch_context"), context={}
            )
        try:
            result = self._call_tool(self.context_tool, {"urn": asset_urn})
            context = _tool_payload(result)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
            return MCPContextResult(
                integration=IntegrationResult(
                    status=IntegrationStatus.FAILED,
                    operation="mcp_fetch_context",
                    message=f"DataHub MCP context retrieval failed: {error}",
                    details={"asset_urn": asset_urn, "tool": self.context_tool},
                )
            )
        return MCPContextResult(
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="mcp_fetch_context",
                message=(
                    "Schema, lineage, glossary and ownership context retrieved "
                    "from DataHub MCP"
                ),
                resource_id=asset_urn,
                evidence_refs=[asset_urn],
                details={"tool": self.context_tool},
            ),
            context=context,
        )

    def write_evidence_report(
        self,
        *,
        asset_urn: str,
        case_id: str,
        report: dict[str, Any],
        degraded: bool,
    ) -> IntegrationResult:
        if not self.endpoint:
            return self._not_configured("mcp_write_evidence")
        arguments = {
            "asset_urn": asset_urn,
            "title": f"DataRescue evidence report {case_id}",
            "content": json.dumps(report, sort_keys=True),
            "tags": ["datarescue:degraded"] if degraded else ["datarescue:patch-validated"],
        }
        try:
            result = self._call_tool(self.write_tool, arguments)
            payload = _tool_payload(result)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
            return IntegrationResult(
                status=IntegrationStatus.FAILED,
                operation="mcp_write_evidence",
                message=f"DataHub MCP evidence write failed: {error}",
                details={"asset_urn": asset_urn, "tool": self.write_tool},
            )
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="mcp_write_evidence",
            message=(
                "Evidence report and degraded marker written to DataHub"
                if degraded
                else "Validated-patch evidence report written to DataHub"
            ),
            resource_id=str(payload.get("urn") or asset_urn),
            evidence_refs=[str(payload.get("urn") or asset_urn)],
            details={"tool": self.write_tool, "degraded": degraded},
        )

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if not self.endpoint:
            raise ValueError("DataHub MCP endpoint is not configured")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(timeout=30, transport=self.transport) as client:
            initialize = client.post(
                self.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "datarescue", "version": "0.1.0"},
                    },
                },
            )
            initialize.raise_for_status()
            initialized = _rpc_body(initialize)
            if initialized.get("error"):
                raise ValueError(initialized["error"])
            session = initialize.headers.get("mcp-session-id")
            if session:
                headers["mcp-session-id"] = session
            notification = client.post(
                self.endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            notification.raise_for_status()
            response = client.post(
                self.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            response.raise_for_status()
            body = _rpc_body(response)
        if body.get("error"):
            raise ValueError(body["error"])
        return body["result"]

    @staticmethod
    def _not_configured(operation: str) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.NOT_CONFIGURED,
            operation=operation,
            message="DATARESCUE_DATAHUB_MCP_URL is not configured; no remote operation was claimed",
        )


def _rpc_body(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("MCP response is not an object")
        return body
    messages: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            parsed = json.loads(line[5:].strip())
            if isinstance(parsed, dict):
                messages.append(parsed)
    if not messages:
        raise ValueError("MCP event stream contained no JSON-RPC message")
    return messages[-1]


def _tool_payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("MCP tool result is not an object")
    if result.get("isError"):
        raise ValueError("MCP tool reported an error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("MCP tool returned no structured context")
