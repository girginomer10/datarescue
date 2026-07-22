from __future__ import annotations

import ipaddress
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field

from apps.api.models import IntegrationResult, IntegrationStatus

CANONICAL_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)"
)
CANONICAL_CONTEXT_DOCUMENT_URN = "urn:li:document:datarescue-net-revenue-contract"
CANONICAL_CONTEXT_DOCUMENT_TITLE = "DataRescue Net Revenue Contract"
CANONICAL_OWNER_URN = "urn:li:corpuser:finance-data"
CANONICAL_OWNER_DISPLAY_NAME = "Finance Data"
CANONICAL_GLOSSARY_TERM_URN = "urn:li:glossaryTerm:NetRevenue"
CANONICAL_DBT_SOURCE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.raw.payments_raw,PROD)"
)
CANONICAL_DBT_STAGING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.stg_payments,PROD)"
)
CANONICAL_PHYSICAL_STAGING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.analytics.stg_payments,PROD)"
)
CANONICAL_DBT_MART_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.fct_revenue,PROD)"
)
CANONICAL_REQUIRED_LINEAGE_URNS = {
    CANONICAL_ASSET_URN,
    CANONICAL_DBT_SOURCE_URN,
    CANONICAL_DBT_STAGING_URN,
    CANONICAL_PHYSICAL_STAGING_URN,
    CANONICAL_DBT_MART_URN,
}
CANONICAL_DIRECT_LINEAGE_EDGES = (
    (CANONICAL_ASSET_URN, CANONICAL_DBT_SOURCE_URN),
    (CANONICAL_DBT_SOURCE_URN, CANONICAL_DBT_STAGING_URN),
    (CANONICAL_DBT_STAGING_URN, CANONICAL_PHYSICAL_STAGING_URN),
    (CANONICAL_PHYSICAL_STAGING_URN, CANONICAL_DBT_MART_URN),
)
# DataHub's dbt ingestion also emits the native physical dependency from the
# raw Postgres relation to the materialized staging relation.  It is a valid
# parallel edge, not a replacement for the four semantic bridge edges above.
CANONICAL_ALLOWED_PARALLEL_LINEAGE_EDGES = frozenset(
    {(CANONICAL_ASSET_URN, CANONICAL_PHYSICAL_STAGING_URN)}
)
MCP_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_READBACK_ATTEMPTS = 5
DEFAULT_READBACK_DELAY_SECONDS = 0.2


class MCPContextResult(BaseModel):
    integration: IntegrationResult
    context: dict[str, Any] = Field(default_factory=dict)


class DataHubMCPAdapter:
    """Small synchronous client for the official DataHub MCP v0.6 tool surface.

    ``get_asset_context`` and ``write_document`` were prototype-only names that do
    not exist in the official server.  Keep the legacy constructor parameters so
    existing settings can be rolled forward without breaking application startup,
    but translate them onto the official composite read and ``save_document``.
    """

    ENTITY_TOOL = "get_entities"
    SCHEMA_TOOL = "list_schema_fields"
    LINEAGE_TOOL = "get_lineage"
    DOCUMENT_TOOL = "save_document"
    DOCUMENT_CONTENT_TOOL = "grep_documents"

    def __init__(
        self,
        *,
        endpoint: str | None,
        token: str | None = None,
        context_tool: str = "get_entities",
        write_tool: str = "save_document",
        transport: httpx.BaseTransport | None = None,
        gms_url: str | None = None,
        gms_token: str | None = None,
        gms_transport: httpx.BaseTransport | None = None,
        readback_attempts: int = DEFAULT_READBACK_ATTEMPTS,
        readback_delay_seconds: float = DEFAULT_READBACK_DELAY_SECONDS,
    ) -> None:
        self.endpoint = _validated_endpoint(endpoint)
        self.token = token
        # WorkflowService still passes the original prototype settings.  The
        # context setting cannot represent the three calls required by the
        # official server, so it is intentionally retained for diagnostics only.
        self.legacy_context_tool = context_tool
        self.write_tool = self.DOCUMENT_TOOL if write_tool == "write_document" else write_tool
        self.transport = transport
        self.gms_url = gms_url.rstrip("/") if gms_url else None
        self.gms_token = gms_token
        self.gms_transport = gms_transport
        if readback_attempts < 1:
            raise ValueError("MCP document read-back attempts must be at least one")
        if readback_delay_seconds < 0:
            raise ValueError("MCP document read-back delay cannot be negative")
        self.readback_attempts = readback_attempts
        self.readback_delay_seconds = readback_delay_seconds

    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        if not self.endpoint:
            return MCPContextResult(
                integration=self._not_configured("mcp_fetch_context"), context={}
            )
        try:
            calls: list[tuple[str, dict[str, Any]]] = [
                (self.ENTITY_TOOL, {"urns": asset_urn}),
                (
                    self.SCHEMA_TOOL,
                    {"urn": asset_urn, "limit": 100, "offset": 0},
                ),
                (
                    self.LINEAGE_TOOL,
                    _lineage_arguments(asset_urn, max_hops=4),
                ),
            ]
            if asset_urn == CANONICAL_ASSET_URN:
                calls.extend(
                    (
                        self.LINEAGE_TOOL,
                        _lineage_arguments(source_urn, max_hops=1),
                    )
                    for source_urn, _target_urn in CANONICAL_DIRECT_LINEAGE_EDGES
                )
            results = self._call_tools(calls)
            entity = _tool_payload(results[0])
            schema = _tool_payload(results[1])
            lineage = _tool_payload(results[2])
            direct_lineages = (
                {
                    source_urn: _tool_payload(result)
                    for (source_urn, _target_urn), result in zip(
                        CANONICAL_DIRECT_LINEAGE_EDGES,
                        results[3:],
                        strict=True,
                    )
                }
                if asset_urn == CANONICAL_ASSET_URN
                else None
            )
            canonical_document_info = (
                self._read_gms_document_info(
                    CANONICAL_CONTEXT_DOCUMENT_URN,
                    title=CANONICAL_CONTEXT_DOCUMENT_TITLE,
                    asset_urn=asset_urn,
                )
                if asset_urn == CANONICAL_ASSET_URN and self.gms_url
                else None
            )
            context = _normalize_context(
                asset_urn=asset_urn,
                entity=entity,
                schema=schema,
                lineage=lineage,
                direct_lineages=direct_lineages,
                canonical_document_info=canonical_document_info,
            )
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
            return MCPContextResult(
                integration=IntegrationResult(
                    status=IntegrationStatus.FAILED,
                    operation="mcp_fetch_context",
                    message=f"DataHub MCP context retrieval failed: {error}",
                    details={
                        "asset_urn": asset_urn,
                        "tools": [
                            self.ENTITY_TOOL,
                            self.SCHEMA_TOOL,
                            self.LINEAGE_TOOL,
                        ],
                    },
                )
            )
        evidence_refs = _unique_strings(
            [asset_urn, *context["lineage_urns"], *context["context_documents"]]
        )
        return MCPContextResult(
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="mcp_fetch_context",
                message=(
                    "Schema, lineage, glossary and ownership context retrieved from DataHub MCP"
                ),
                resource_id=asset_urn,
                evidence_refs=evidence_refs,
                details={
                    "tools": [
                        self.ENTITY_TOOL,
                        self.SCHEMA_TOOL,
                        self.LINEAGE_TOOL,
                    ],
                    "schema_field_count": len(context["schema_fields"]),
                    "lineage_entity_count": len(context["lineage_urns"]),
                },
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
        title = f"DataRescue evidence report {case_id}"
        content = json.dumps(report, sort_keys=True)
        arguments = {
            "document_type": "Analysis",
            "title": title,
            "content": content,
            "topics": ["datarescue", "degraded"] if degraded else ["datarescue", "patch-validated"],
            "related_assets": [asset_urn],
        }
        try:
            result = self._call_tool(self.write_tool, arguments)
            payload = _tool_payload(result)
            document_urn = _validated_document_result(payload)
            self._verify_document_readback(
                document_urn=document_urn,
                title=title,
                content=content,
                asset_urn=asset_urn,
            )
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
            resource_id=document_urn,
            evidence_refs=[document_urn, asset_urn],
            details={
                "tool": self.write_tool,
                "degraded": degraded,
                "readback_verified": True,
            },
        )

    def _verify_document_readback(
        self,
        *,
        document_urn: str,
        title: str,
        content: str,
        asset_urn: str,
    ) -> None:
        if self.gms_url:
            self._read_gms_document_info(
                document_urn,
                title=title,
                content=content,
                asset_urn=asset_urn,
            )
            return
        last_error: Exception | None = None
        for attempt in range(1, self.readback_attempts + 1):
            try:
                content_result, asset_result = self._call_tools(
                    [
                        (
                            self.DOCUMENT_CONTENT_TOOL,
                            {
                                "urns": [document_urn],
                                "pattern": f"^{re.escape(content)}$",
                                "context_chars": 0,
                                "max_matches_per_doc": 1,
                                "start_offset": 0,
                            },
                        ),
                        (self.ENTITY_TOOL, {"urns": asset_urn}),
                    ]
                )
                _validate_document_readback(
                    content_search=_tool_payload(content_result),
                    asset_entity=_tool_payload(asset_result),
                    document_urn=document_urn,
                    title=title,
                    content=content,
                    asset_urn=asset_urn,
                )
                return
            except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
                last_error = error
            if attempt < self.readback_attempts and self.readback_delay_seconds:
                time.sleep(self.readback_delay_seconds)
        raise ValueError(
            "save_document was not confirmed by an exact DataHub read-back "
            f"after {self.readback_attempts} attempt(s): {last_error}"
        )

    def _read_gms_document_info(
        self,
        document_urn: str,
        *,
        title: str,
        asset_urn: str,
        content: str | None = None,
    ) -> dict[str, Any]:
        if not self.gms_url:
            raise ValueError("DataHub GMS is not configured for document read-back")
        if not document_urn.startswith("urn:li:document:"):
            raise ValueError("Document read-back requires a document URN")
        headers: dict[str, str] = {}
        if self.gms_token:
            headers["Authorization"] = f"Bearer {self.gms_token}"
        endpoint = (
            f"{self.gms_url}/openapi/v3/entity/document/"
            f"{quote(document_urn, safe='')}/documentInfo"
        )
        last_error: Exception | None = None
        for attempt in range(1, self.readback_attempts + 1):
            try:
                with httpx.Client(timeout=20, transport=self.gms_transport) as client:
                    response = client.get(endpoint, headers=headers)
                    response.raise_for_status()
                    body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("documentInfo read-back returned no value object")
                raw_info = body.get("value")
                if not isinstance(raw_info, dict):
                    raise ValueError("documentInfo read-back returned no value object")
                info: dict[str, Any] = raw_info
                _validate_gms_document_info(
                    info,
                    title=title,
                    content=content,
                    asset_urn=asset_urn,
                )
                return info
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as error:
                last_error = error
            if attempt < self.readback_attempts and self.readback_delay_seconds:
                time.sleep(self.readback_delay_seconds)
        raise ValueError(
            "documentInfo was not confirmed by direct DataHub read-back "
            f"after {self.readback_attempts} attempt(s): {last_error}"
        )

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._call_tools([(name, arguments)])[0]

    def _call_tools(self, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        if not self.endpoint:
            raise ValueError("DataHub MCP endpoint is not configured")
        if not calls:
            return []
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
            initialized = _rpc_body(initialize, request_id=1)
            if initialized.get("error"):
                raise ValueError(initialized["error"])
            initialize_result = initialized.get("result")
            negotiated_protocol = _validated_initialize_result(initialize_result)
            headers["MCP-Protocol-Version"] = negotiated_protocol
            session = initialize.headers.get("mcp-session-id")
            if session:
                headers["MCP-Session-Id"] = session
            notification = client.post(
                self.endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            notification.raise_for_status()
            results: list[Any] = []
            for call_id, (name, arguments) in enumerate(calls, start=2):
                response = client.post(
                    self.endpoint,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                response.raise_for_status()
                body = _rpc_body(response, request_id=call_id)
                if body.get("error"):
                    raise ValueError(body["error"])
                if "result" not in body:
                    raise ValueError("MCP JSON-RPC response returned no result")
                results.append(body["result"])
        return results

    @staticmethod
    def _not_configured(operation: str) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.NOT_CONFIGURED,
            operation=operation,
            message="DATARESCUE_DATAHUB_MCP_URL is not configured; no remote operation was claimed",
        )


def _rpc_body(response: httpx.Response, *, request_id: int | str) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").casefold()
    if "text/event-stream" not in content_type:
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("MCP response is not an object")
        return _validated_rpc_response(body, request_id=request_id)

    for event_data in _sse_data_frames(response.text):
        parsed = json.loads(event_data)
        if not isinstance(parsed, dict) or parsed.get("id") != request_id:
            continue
        return _validated_rpc_response(parsed, request_id=request_id)
    raise ValueError(f"MCP event stream contained no response for request id {request_id!r}")


def _sse_data_frames(payload: str) -> list[str]:
    frames: list[str] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            frames.append("\n".join(data_lines))
            data_lines.clear()

    for raw_line in payload.splitlines():
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


def _validated_rpc_response(body: dict[str, Any], *, request_id: int | str) -> dict[str, Any]:
    if body.get("jsonrpc") != "2.0":
        raise ValueError("MCP response has an invalid JSON-RPC version")
    if body.get("id") != request_id:
        raise ValueError(
            f"MCP response id {body.get('id')!r} does not match request id {request_id!r}"
        )
    if "result" not in body and "error" not in body:
        raise ValueError("MCP JSON-RPC response has neither result nor error")
    return body


def _validated_initialize_result(result: Any) -> str:
    if not isinstance(result, dict):
        raise ValueError("MCP initialize returned no result object")
    protocol = result.get("protocolVersion")
    if protocol != MCP_PROTOCOL_VERSION:
        raise ValueError(
            f"MCP negotiated unsupported protocol version {protocol!r}; "
            f"expected {MCP_PROTOCOL_VERSION}"
        )
    capabilities = result.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("MCP initialize returned no capabilities object")
    server_info = result.get("serverInfo")
    if not isinstance(server_info, dict):
        raise ValueError("MCP initialize returned no serverInfo object")
    if not isinstance(server_info.get("name"), str) or not server_info["name"].strip():
        raise ValueError("MCP initialize returned invalid serverInfo.name")
    return MCP_PROTOCOL_VERSION


def _tool_payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("MCP tool result is not an object")
    if result.get("isError"):
        raise ValueError("MCP tool reported an error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        meta = result.get("_meta")
        fastmcp = meta.get("fastmcp") if isinstance(meta, dict) else None
        wrapped = isinstance(fastmcp, dict) and fastmcp.get("wrap_result") is True
        if wrapped or set(structured) == {"result"}:
            unwrapped = structured.get("result")
            if not isinstance(unwrapped, dict):
                raise ValueError("MCP structured result is not an object")
            return unwrapped
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
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("MCP tool returned no structured object")


def _normalize_context(
    *,
    asset_urn: str,
    entity: dict[str, Any],
    schema: dict[str, Any],
    lineage: dict[str, Any],
    direct_lineages: dict[str, dict[str, Any]] | None = None,
    canonical_document_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if entity.get("error"):
        raise ValueError(f"get_entities failed for {asset_urn}")
    if entity.get("urn") != asset_urn:
        raise ValueError("get_entities returned a different or missing asset URN")

    schema_fields = _validated_schema_fields(asset_urn, schema)
    lineage_urns = _validated_lineage_urns(asset_urn, lineage)
    glossary_definition, glossary_urns = _glossary_context(entity)
    owner = _owner_context(entity)
    document_urns = _document_context(entity)
    if asset_urn == CANONICAL_ASSET_URN:
        if canonical_document_info is not None:
            _validate_gms_document_info(
                canonical_document_info,
                title=CANONICAL_CONTEXT_DOCUMENT_TITLE,
                asset_urn=asset_urn,
            )
            document_urns = _unique_strings(
                [*document_urns, CANONICAL_CONTEXT_DOCUMENT_URN]
            )
        if not _canonical_owner_verified(entity):
            raise ValueError("canonical DataRescue asset owner does not match the exact contract")
        canonical_glossary_definition = _canonical_glossary_definition(entity)
        if canonical_glossary_definition is None:
            raise ValueError("canonical DataRescue NetRevenue glossary contract is unavailable")
        if CANONICAL_CONTEXT_DOCUMENT_URN not in document_urns:
            raise ValueError("canonical DataRescue context document is unavailable")
        # Expose only the exact seeded semantic term and owner to downstream
        # candidate generation. Unrelated metadata must not become evidence.
        glossary_definition = canonical_glossary_definition
        owner = CANONICAL_OWNER_DISPLAY_NAME
    context_documents = _unique_strings([*glossary_urns, *document_urns])
    if asset_urn == CANONICAL_ASSET_URN:
        topology_current = _canonical_topology_current(direct_lineages)
        lineage_current = (
            CANONICAL_REQUIRED_LINEAGE_URNS.issubset(lineage_urns) and topology_current
        )
    else:
        lineage_current = True

    return {
        "asset_urn": asset_urn,
        "schema_fields": schema_fields,
        "glossary_definition": glossary_definition,
        "owner": owner,
        "lineage_urns": lineage_urns,
        "context_documents": context_documents,
        # For the only asset supported by v1, current means the complete,
        # ingestion-verified physical -> dbt -> materialized lineage contract
        # was returned in this live request. Any missing hop fails closed.
        "lineage_current": lineage_current,
    }


def _validated_schema_fields(asset_urn: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("urn") != asset_urn:
        raise ValueError("list_schema_fields returned a different or missing asset URN")
    fields = payload.get("fields")
    if not isinstance(fields, list):
        raise ValueError("list_schema_fields returned no fields array")
    if not fields:
        raise ValueError("list_schema_fields returned no schema fields")
    if any(
        not isinstance(field, dict)
        or not isinstance(field.get("fieldPath"), str)
        or not field["fieldPath"].strip()
        for field in fields
    ):
        raise ValueError("list_schema_fields returned an invalid field")
    returned = payload.get("returned")
    total = payload.get("totalFields")
    remaining = payload.get("remainingCount")
    if (
        not _plain_int(returned)
        or not _plain_int(total)
        or not _plain_int(remaining)
        or returned != len(fields)
        or total < returned
        or remaining != total - returned
    ):
        raise ValueError("list_schema_fields returned inconsistent pagination metadata")
    if remaining:
        raise ValueError("list_schema_fields returned a partial schema")
    return fields


def _validated_lineage_urns(asset_urn: str, payload: dict[str, Any]) -> list[str]:
    results = _validated_lineage_results(payload)
    return _unique_strings([asset_urn, *(urn for urn, _degree in results)])


def _validated_lineage_results(payload: dict[str, Any]) -> list[tuple[str, int]]:
    downstreams = payload.get("downstreams")
    if not isinstance(downstreams, dict):
        raise ValueError("get_lineage returned no downstream lineage object")
    results = downstreams.get("searchResults")
    if not isinstance(results, list):
        raise ValueError("get_lineage returned no downstream searchResults array")
    if not results:
        raise ValueError("get_lineage returned no downstream lineage")
    if downstreams.get("hasMore") is True or downstreams.get("truncatedDueToTokenBudget") is True:
        raise ValueError("get_lineage returned a partial lineage graph")
    total = downstreams.get("total")
    returned = downstreams.get("returned")
    if (
        not _plain_int(total)
        or not _plain_int(returned)
        or total != len(results)
        or returned != len(results)
    ):
        raise ValueError("get_lineage returned incomplete or invalid result metadata")

    normalized: list[tuple[str, int]] = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("get_lineage returned an invalid search result")
        entity = result.get("entity")
        if not isinstance(entity, dict):
            raise ValueError("get_lineage search result has no entity")
        urn = entity.get("urn")
        if not isinstance(urn, str) or not urn.startswith("urn:li:"):
            raise ValueError("get_lineage search result has no valid entity URN")
        degree = result.get("degree")
        if not isinstance(degree, int) or isinstance(degree, bool) or degree < 1:
            raise ValueError("get_lineage search result has no valid positive degree")
        normalized.append((urn, degree))
    return normalized


def _canonical_topology_current(
    direct_lineages: dict[str, dict[str, Any]] | None,
) -> bool:
    if direct_lineages is None or set(direct_lineages) != {
        source_urn for source_urn, _target_urn in CANONICAL_DIRECT_LINEAGE_EDGES
    }:
        return False
    for source_urn, target_urn in CANONICAL_DIRECT_LINEAGE_EDGES:
        results = _validated_lineage_results(direct_lineages[source_urn])
        canonical_results = {
            (urn, degree)
            for urn, degree in results
            if urn in CANONICAL_REQUIRED_LINEAGE_URNS
        }
        allowed_targets = {target_urn} | {
            parallel_target
            for parallel_source, parallel_target in CANONICAL_ALLOWED_PARALLEL_LINEAGE_EDGES
            if parallel_source == source_urn
        }
        if (target_urn, 1) not in canonical_results or any(
            degree != 1 or urn not in allowed_targets
            for urn, degree in canonical_results
        ):
            return False
    return True


def _glossary_context(entity: dict[str, Any]) -> tuple[str | None, list[str]]:
    glossary = entity.get("glossaryTerms")
    if glossary is None:
        return None, []
    if not isinstance(glossary, dict) or not isinstance(glossary.get("terms"), list):
        raise ValueError("get_entities returned invalid glossary terms")

    definitions: list[str] = []
    urns: list[str] = []
    for association in glossary["terms"]:
        if not isinstance(association, dict) or not isinstance(association.get("term"), dict):
            raise ValueError("get_entities returned an invalid glossary association")
        term = association["term"]
        urn = term.get("urn")
        if not isinstance(urn, str) or not urn.startswith("urn:li:glossaryTerm:"):
            raise ValueError("get_entities returned an invalid glossary term URN")
        urns.append(urn)
        properties = term.get("properties")
        if properties is not None and not isinstance(properties, dict):
            raise ValueError("get_entities returned invalid glossary term properties")
        if isinstance(properties, dict):
            description = properties.get("description")
            if isinstance(description, str) and description.strip():
                definitions.append(description.strip())
    return "\n\n".join(_unique_strings(definitions)) or None, _unique_strings(urns)


def _canonical_glossary_definition(entity: dict[str, Any]) -> str | None:
    glossary = entity.get("glossaryTerms")
    if not isinstance(glossary, dict) or not isinstance(glossary.get("terms"), list):
        return None
    for association in glossary["terms"]:
        if not isinstance(association, dict):
            continue
        term = association.get("term")
        if not isinstance(term, dict) or term.get("urn") != CANONICAL_GLOSSARY_TERM_URN:
            continue
        description = _nested_string(term, "properties", "description")
        if description:
            return description
    return None


def _owner_context(entity: dict[str, Any]) -> str | None:
    ownership = entity.get("ownership")
    if ownership is None:
        return None
    if not isinstance(ownership, dict) or not isinstance(ownership.get("owners"), list):
        raise ValueError("get_entities returned invalid ownership")
    for association in ownership["owners"]:
        if not isinstance(association, dict) or not isinstance(association.get("owner"), dict):
            raise ValueError("get_entities returned an invalid owner association")
        owner = association["owner"]
        for path in (
            ("editableProperties", "displayName"),
            ("properties", "displayName"),
            ("info", "displayName"),
            ("properties", "email"),
        ):
            value = _nested_string(owner, *path)
            if value:
                return value
        for key in ("name", "urn"):
            value = owner.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _canonical_owner_verified(entity: dict[str, Any]) -> bool:
    ownership = entity.get("ownership")
    if not isinstance(ownership, dict) or not isinstance(ownership.get("owners"), list):
        return False
    for association in ownership["owners"]:
        if not isinstance(association, dict):
            continue
        owner = association.get("owner")
        if not isinstance(owner, dict) or owner.get("urn") != CANONICAL_OWNER_URN:
            continue
        display_name = next(
            (
                value
                for path in (
                    ("editableProperties", "displayName"),
                    ("properties", "displayName"),
                    ("info", "displayName"),
                )
                if (value := _nested_string(owner, *path))
            ),
            None,
        )
        if display_name == CANONICAL_OWNER_DISPLAY_NAME:
            return True
    return False


def _document_context(entity: dict[str, Any]) -> list[str]:
    related = entity.get("relatedDocuments")
    if related is None:
        return []
    if not isinstance(related, dict) or not isinstance(related.get("documents"), list):
        raise ValueError("get_entities returned invalid related documents")
    urns: list[str] = []
    for document in related["documents"]:
        if not isinstance(document, dict):
            raise ValueError("get_entities returned an invalid related document")
        urn = document.get("urn")
        if not isinstance(urn, str) or not urn.startswith("urn:li:document:"):
            raise ValueError("get_entities returned an invalid document URN")
        urns.append(urn)
    return _unique_strings(urns)


def _validated_document_result(payload: dict[str, Any]) -> str:
    if payload.get("success") is not True:
        message = payload.get("message")
        suffix = f": {message}" if isinstance(message, str) and message else ""
        raise ValueError(f"save_document did not confirm success{suffix}")
    urn = payload.get("urn")
    if not isinstance(urn, str) or not urn.startswith("urn:li:document:"):
        raise ValueError("save_document did not return a valid document URN")
    return urn


def _validate_document_readback(
    *,
    content_search: dict[str, Any],
    asset_entity: dict[str, Any],
    document_urn: str,
    title: str,
    content: str,
    asset_urn: str,
) -> None:
    if content_search.get("documents_with_matches") != 1:
        raise ValueError("grep_documents did not find the exact saved document content")
    if content_search.get("total_matches") != 1:
        raise ValueError("grep_documents returned an ambiguous document content match")
    results = content_search.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("grep_documents returned an invalid result set")
    result = results[0]
    if not isinstance(result, dict) or result.get("urn") != document_urn:
        raise ValueError("grep_documents returned a different or missing document URN")
    if result.get("title") != title:
        raise ValueError("saved document title does not match the requested evidence")
    if result.get("total_matches") != 1:
        raise ValueError("saved document content match count is not exact")
    matches = result.get("matches")
    if not isinstance(matches, list) or len(matches) != 1:
        raise ValueError("saved document has no exact content match")
    match = matches[0]
    if (
        not isinstance(match, dict)
        or match.get("position") != 0
        or match.get("excerpt") != content
    ):
        raise ValueError("saved document content does not match the requested evidence")
    if asset_entity.get("urn") != asset_urn:
        raise ValueError("get_entities returned a different or missing asset URN")
    if document_urn not in _document_context(asset_entity):
        raise ValueError("saved document is not linked to the requested DataHub asset")


def _validate_gms_document_info(
    info: dict[str, Any],
    *,
    title: str,
    asset_urn: str,
    content: str | None = None,
) -> None:
    if info.get("title") != title:
        raise ValueError("saved document title does not match the requested evidence")
    if content is not None:
        contents = info.get("contents")
        if not isinstance(contents, dict) or contents.get("text") != content:
            raise ValueError("saved document content does not match the requested evidence")
    related_assets = info.get("relatedAssets")
    if not isinstance(related_assets, list):
        raise ValueError("saved document has no relatedAssets array")
    linked_urns = {
        linked
        for association in related_assets
        if isinstance(association, dict)
        and isinstance((linked := association.get("asset")), str)
    }
    if asset_urn not in linked_urns:
        raise ValueError("saved document is not linked to the requested DataHub asset")


def _lineage_arguments(asset_urn: str, *, max_hops: int) -> dict[str, Any]:
    return {
        "urn": asset_urn,
        "upstream": False,
        "max_hops": max_hops,
        "max_results": 100,
        "offset": 0,
    }


def _validated_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    normalized = endpoint.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("DataHub MCP endpoint must not embed credentials")
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("DataHub MCP endpoint must be an absolute HTTP(S) URL")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("non-loopback DataHub MCP endpoints must use HTTPS")
    return normalized


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _nested_string(value: dict[str, Any], *path: str) -> str | None:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
