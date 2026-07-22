from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from apps.api.config import CANONICAL_ASSET_URN

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify-connected-case.py"
SPEC = importlib.util.spec_from_file_location("verify_connected_case", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _integration(
    operation: str,
    *,
    resource_id: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": "SUCCEEDED",
        "operation": operation,
        "message": "verified",
        "resource_id": resource_id,
        "details": details or {},
    }


def _candidate(source_field: str, outcome: str, variance: float) -> dict[str, object]:
    return {
        "id": f"candidate-{source_field}",
        "source_field": source_field,
        "target_alias": "revenue",
        "rationale": "evidence-backed candidate",
        "semantic_verdict": "MATCH" if source_field == "net_amount" else "CONFLICT",
        "evidence_refs": ["urn:li:glossaryTerm:NetRevenue"],
        "reconciliation": {
            "total_variance_pct": variance,
            "row_count_variance_pct": 0.0,
            "primary_key_overlap_pct": 100.0,
            "null_rate_delta_percentage_points": 0.0,
        },
        "build": {
            "passed": True,
            "passed_checks": 8,
            "total_checks": 8,
            "command": "dbt build",
        },
        "outcome": outcome,
    }


def _case(*, state: str = "PR_OPEN") -> dict[str, Any]:
    incident_urn = "urn:li:incident:connected-proof"
    pr_url = "https://github.com/girginomer10/datarescue/pull/42"
    gross = _candidate("gross_amount", "REJECTED", 3.4)
    net = _candidate("net_amount", "SELECTED", 0.0)
    return {
        "id": "DR-CONNECTED",
        "asset_urn": CANONICAL_ASSET_URN,
        "state": state,
        "incident_status": "ACTIVE",
        "incident_integration": _integration(
            "graphql_raise_incident",
            resource_id=incident_urn,
            details={"readback_verified": True, "remote_state": "ACTIVE"},
        ),
        "schema_change": {
            "source": "DATAHUB_MCL",
            "before_fields": [{"name": "payment_id"}, {"name": "amount"}],
            "after_fields": [
                {"name": "payment_id"},
                {"name": "gross_amount"},
                {"name": "net_amount"},
            ],
        },
        "context": {
            "source": "DATAHUB_MCP",
            "integration": _integration("mcp_fetch_context"),
        },
        "candidate_generation": _integration("candidate_generation"),
        "candidates": [gross, net],
        "selected_candidate": net,
        "evidence_writeback": _integration(
            "mcp_write_evidence",
            resource_id="urn:li:document:datarescue-proof",
            details={"readback_verified": True, "degraded": False},
        ),
        "pull_request": {
            "url": pr_url,
            "branch": "datarescue/dr-connected",
            "integration": _integration(
                "github_create_draft_pr",
                resource_id=pr_url,
                details={"requires_human_merge": True},
            ),
        },
        "containment_reasons": [],
    }


def test_validator_accepts_only_the_complete_connected_pr_open_contract() -> None:
    proof = MODULE.validate_current_cases([_case()])

    assert proof == {
        "case_id": "DR-CONNECTED",
        "incident_urn": "urn:li:incident:connected-proof",
        "gross_variance_pct": 3.4,
        "selected_candidate": "net_amount",
        "selected_variance_pct": 0.0,
        "primary_key_overlap_pct": 100.0,
        "dbt_checks": "8/8",
        "pr_url": "https://github.com/girginomer10/datarescue/pull/42",
        "state": "PR_OPEN",
    }


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            lambda case: case["incident_integration"]["details"].update(
                {"remote_state": "RESOLVED"}
            ),
            "remote ACTIVE",
        ),
        (
            lambda case: case["candidates"][0]["reconciliation"].update(
                {"total_variance_pct": 0.0}
            ),
            "must be 3.40",
        ),
        (
            lambda case: case["selected_candidate"]["build"].update({"passed_checks": 7}),
            "8/8 dbt checks",
        ),
        (
            lambda case: case["evidence_writeback"]["details"].update({"readback_verified": False}),
            "no exact read-back proof",
        ),
        (
            lambda case: case["pull_request"]["integration"]["details"].update(
                {"requires_human_merge": False}
            ),
            "human merge",
        ),
    ],
)
def test_validator_rejects_unproven_success_claims(mutation: Any, match: str) -> None:
    case = _case()
    mutation(case)

    with pytest.raises(MODULE.ProofError, match=match):
        MODULE.validate_case(case)


@pytest.mark.parametrize("state", ["CONTAINED", "FAILED"])
def test_fail_closed_terminal_states_abort_without_waiting(state: str) -> None:
    case = _case(state=state)
    case["containment_reasons"] = ["no safe connected candidate"]

    with pytest.raises(MODULE.TerminalProofError, match=state):
        MODULE.validate_current_cases([case])


def test_poll_waits_through_progress_but_returns_only_full_proof() -> None:
    responses: list[object] = [[], [_case(state="VALIDATING")], [_case()]]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == "/api/v1/cases"
        payload = responses[calls]
        calls += 1
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        proof = MODULE.poll(
            "http://api.test",
            1,
            interval_seconds=0,
            client=client,
        )

    assert proof["state"] == "PR_OPEN"
    assert calls == 3


def test_poll_aborts_immediately_when_the_case_is_contained() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        case = _case(state="CONTAINED")
        case["containment_reasons"] = ["gross candidate breached the variance policy"]
        return httpx.Response(200, json=[case])

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(MODULE.TerminalProofError, match="CONTAINED"),
    ):
        MODULE.poll(
            "http://api.test",
            10,
            interval_seconds=0,
            client=client,
        )

    assert calls == 1


def test_launcher_resets_scope_then_requires_bounded_pr_open_proof() -> None:
    launcher = (REPO_ROOT / "scripts" / "demo-connected.sh").read_text(encoding="utf-8")
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    api_ready = launcher.index("wait_for_api\n")
    reset = launcher.index('"${DATARESCUE_API_URL}/api/v1/demo/reset"', api_ready)
    actions = launcher.index("bash scripts/datahub-actions.sh run", reset)
    drift = launcher.index("make datahub-apply-drift", actions)
    proof = launcher.index("scripts/verify-connected-case.py", drift)
    web = launcher.index("apps/web/node_modules/.bin/vite", proof)

    assert api_ready < reset < actions < drift < proof < web
    assert '--timeout "${DATARESCUE_CONNECTED_PROOF_TIMEOUT_SECONDS}"' in launcher
    assert "DATARESCUE_CONNECTED_PROOF_TIMEOUT_SECONDS=600" in example
    assert 'DATAHUB_ACTIONS_GROUP="${DATAHUB_ACTIONS_GROUP:-datarescue-connected-$$}"' in launcher
    assert 'gms_url=os.environ["DATARESCUE_DATAHUB_GMS_URL"]' in launcher
    assert 'gms_token=os.environ.get("DATARESCUE_DATAHUB_TOKEN") or None' in launcher
