#!/usr/bin/env python3
"""Verify DataRescue's official DataHub MCP reads and evidence write-back."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any

from apps.api.config import CANONICAL_ASSET_URN
from apps.api.models import IntegrationResult, IntegrationStatus
from packages.datahub.mcp import (
    CANONICAL_CONTEXT_DOCUMENT_URN,
    CANONICAL_REQUIRED_LINEAGE_URNS,
    DataHubMCPAdapter,
    MCPContextResult,
)


class ProofError(AssertionError):
    """Raised when live MCP evidence does not meet the connected contract."""


def validate_context(result: MCPContextResult) -> dict[str, Any]:
    if result.integration.status is not IntegrationStatus.SUCCEEDED:
        raise ProofError(f"MCP context read failed: {result.integration.message}")
    context = result.context
    if context.get("asset_urn") != CANONICAL_ASSET_URN:
        raise ProofError("MCP returned the wrong monitored asset")

    fields = context.get("schema_fields")
    if not isinstance(fields, list):
        raise ProofError("MCP returned no schema fields")
    field_names = {
        field.get("fieldPath")
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("fieldPath"), str)
    }
    common = {"payment_id", "customer_id", "paid_at", "currency", "status"}
    if not common.issubset(field_names):
        raise ProofError(f"MCP schema is missing required fields: {common - field_names}")
    healthy = "amount" in field_names and not {"gross_amount", "net_amount"} & field_names
    drifted = "amount" not in field_names and {"gross_amount", "net_amount"}.issubset(field_names)
    if not (healthy or drifted):
        raise ProofError(f"MCP schema is neither healthy nor the expected drift: {field_names}")

    lineage = context.get("lineage_urns")
    if not isinstance(lineage, list):
        raise ProofError("MCP returned no lineage URNs")
    missing_lineage = CANONICAL_REQUIRED_LINEAGE_URNS - set(lineage)
    if missing_lineage or context.get("lineage_current") is not True:
        raise ProofError(f"MCP lineage contract is incomplete: {sorted(missing_lineage)}")
    if context.get("owner") != "Finance Data":
        raise ProofError(f"MCP returned an unexpected owner: {context.get('owner')!r}")

    glossary = context.get("glossary_definition")
    if not isinstance(glossary, str) or not all(
        token in glossary for token in ("net_amount", "gross_amount")
    ):
        raise ProofError("MCP returned no exact NetRevenue semantic rule")
    documents = context.get("context_documents")
    if not isinstance(documents, list) or CANONICAL_CONTEXT_DOCUMENT_URN not in documents:
        raise ProofError("MCP did not return the deterministic DataRescue context document")

    return {
        "asset_urn": CANONICAL_ASSET_URN,
        "catalog_state": "DRIFTED" if drifted else "HEALTHY",
        "schema_field_count": len(fields),
        "lineage_entity_count": len(lineage),
        "owner": context["owner"],
        "context_document": CANONICAL_CONTEXT_DOCUMENT_URN,
    }


def validate_writeback(result: IntegrationResult) -> str:
    if result.status is not IntegrationStatus.SUCCEEDED:
        raise ProofError(f"MCP evidence write-back failed: {result.message}")
    urn = result.resource_id
    if not isinstance(urn, str) or not urn.startswith("urn:li:document:"):
        raise ProofError("MCP evidence write-back returned no document URN")
    if CANONICAL_ASSET_URN not in result.evidence_refs:
        raise ProofError("MCP evidence write-back is not linked to the monitored asset")
    if result.details.get("readback_verified") is not True:
        raise ProofError("MCP evidence write-back has no exact DataHub read-back proof")
    return urn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=os.getenv("DATARESCUE_DATAHUB_MCP_URL", "http://127.0.0.1:8001/mcp"),
    )
    parser.add_argument("--token", default=os.getenv("DATARESCUE_DATAHUB_MCP_TOKEN"))
    parser.add_argument(
        "--gms-url",
        default=os.getenv("DATARESCUE_DATAHUB_GMS_URL", "http://127.0.0.1:18080"),
    )
    parser.add_argument("--gms-token", default=os.getenv("DATARESCUE_DATAHUB_TOKEN"))
    parser.add_argument("--skip-write", action="store_true")
    args = parser.parse_args()

    adapter = DataHubMCPAdapter(
        endpoint=args.endpoint,
        token=args.token or None,
        gms_url=args.gms_url or None,
        gms_token=args.gms_token or None,
    )
    summary = validate_context(adapter.fetch_context(CANONICAL_ASSET_URN))
    if not args.skip_write:
        case_id = f"DR-MCP-PROOF-{uuid.uuid4().hex[:8].upper()}"
        writeback = adapter.write_evidence_report(
            asset_urn=CANONICAL_ASSET_URN,
            case_id=case_id,
            report={
                "decision": "MCP_LIVE_PROOF",
                "context_document": CANONICAL_CONTEXT_DOCUMENT_URN,
                "lineage_current": True,
            },
            degraded=False,
        )
        summary["writeback_document"] = validate_writeback(writeback)
        summary["writeback_status"] = writeback.status.value
    else:
        summary["writeback_status"] = "SKIPPED"
    summary["context_status"] = IntegrationStatus.SUCCEEDED.value
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
