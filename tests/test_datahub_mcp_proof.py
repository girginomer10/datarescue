from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from apps.api.config import CANONICAL_ASSET_URN
from apps.api.models import IntegrationResult, IntegrationStatus
from packages.datahub.mcp import (
    CANONICAL_CONTEXT_DOCUMENT_URN,
    CANONICAL_REQUIRED_LINEAGE_URNS,
    MCPContextResult,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify-datahub-mcp-proof.py"
SPEC = importlib.util.spec_from_file_location("verify_datahub_mcp_proof", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _context(*, lineage_current: bool = True) -> MCPContextResult:
    return MCPContextResult(
        integration=IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="mcp_fetch_context",
            message="live",
        ),
        context={
            "asset_urn": CANONICAL_ASSET_URN,
            "schema_fields": [
                {"fieldPath": name}
                for name in (
                    "payment_id",
                    "customer_id",
                    "paid_at",
                    "currency",
                    "status",
                    "gross_amount",
                    "net_amount",
                )
            ],
            "lineage_urns": sorted(CANONICAL_REQUIRED_LINEAGE_URNS),
            "lineage_current": lineage_current,
            "owner": "Finance Data",
            "glossary_definition": "Use net_amount and reject gross_amount.",
            "context_documents": [CANONICAL_CONTEXT_DOCUMENT_URN],
        },
    )


def test_mcp_proof_validator_accepts_the_exact_live_contract() -> None:
    summary = MODULE.validate_context(_context())

    assert summary["catalog_state"] == "DRIFTED"
    assert summary["context_document"] == CANONICAL_CONTEXT_DOCUMENT_URN


def test_mcp_proof_validator_fails_closed_on_stale_lineage() -> None:
    with pytest.raises(MODULE.ProofError, match="lineage contract"):
        MODULE.validate_context(_context(lineage_current=False))


def test_mcp_proof_validator_requires_confirmed_asset_linked_writeback() -> None:
    failed = IntegrationResult(
        status=IntegrationStatus.FAILED,
        operation="mcp_write_evidence",
        message="denied",
    )
    with pytest.raises(MODULE.ProofError, match="write-back failed"):
        MODULE.validate_writeback(failed)

    succeeded = IntegrationResult(
        status=IntegrationStatus.SUCCEEDED,
        operation="mcp_write_evidence",
        message="created",
        resource_id="urn:li:document:proof",
        evidence_refs=["urn:li:document:proof", CANONICAL_ASSET_URN],
        details={"readback_verified": True},
    )
    assert MODULE.validate_writeback(succeeded) == "urn:li:document:proof"


def test_mcp_proof_validator_requires_exact_readback_confirmation() -> None:
    unconfirmed = IntegrationResult(
        status=IntegrationStatus.SUCCEEDED,
        operation="mcp_write_evidence",
        message="created",
        resource_id="urn:li:document:proof",
        evidence_refs=["urn:li:document:proof", CANONICAL_ASSET_URN],
    )

    with pytest.raises(MODULE.ProofError, match="read-back proof"):
        MODULE.validate_writeback(unconfirmed)


@pytest.mark.parametrize(
    ("dedicated_token", "expected_token"),
    [(None, None), ("mcp-secret", "mcp-secret")],
)
def test_mcp_proof_never_falls_back_to_the_gms_token(
    monkeypatch: pytest.MonkeyPatch,
    dedicated_token: str | None,
    expected_token: str | None,
) -> None:
    captured: dict[str, object] = {}

    class FakeAdapter:
        def __init__(
            self,
            *,
            endpoint: str,
            token: str | None,
            gms_url: str | None,
            gms_token: str | None,
        ) -> None:
            captured.update(
                endpoint=endpoint,
                token=token,
                gms_url=gms_url,
                gms_token=gms_token,
            )

        def fetch_context(self, asset_urn: str) -> MCPContextResult:
            assert asset_urn == CANONICAL_ASSET_URN
            return _context()

    monkeypatch.setenv("DATARESCUE_DATAHUB_TOKEN", "gms-secret")
    if dedicated_token is None:
        monkeypatch.delenv("DATARESCUE_DATAHUB_MCP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DATARESCUE_DATAHUB_MCP_TOKEN", dedicated_token)
    monkeypatch.setattr(MODULE, "DataHubMCPAdapter", FakeAdapter)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--skip-write"])

    assert MODULE.main() == 0
    assert captured["token"] == expected_token
    assert captured["gms_url"] == "http://127.0.0.1:18080"
    assert captured["gms_token"] == "gms-secret"
