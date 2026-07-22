from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.cli import CONTAINED_EXIT_CODE
from apps.api.cli import main as cli_main
from apps.api.main import create_app
from apps.api.models import (
    CandidateProposal,
    ContextBundle,
    IntegrationResult,
    IntegrationStatus,
    PullRequestArtifact,
)
from apps.api.workflow import DEFAULT_ASSET_URN, WorkflowService
from packages.datahub.mcp import MCPContextResult
from packages.evidence.executor import CandidateExecution, ReplayEvidenceExecutor
from packages.remediation.candidates import CandidateGenerationResult
from tests.backend_helpers import make_test_settings


def test_replay_vertical_slice_rejects_gross_and_selects_net(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/demo/drift", json={})
        assert response.status_code == 200
        body = response.json()
        case = body["case"]

        assert case["state"] == "PATCH_READY"
        assert case["incident_status"] == "ACTIVE"
        by_field = {item["source_field"]: item for item in case["candidates"]}
        assert by_field["gross_amount"]["outcome"] == "REJECTED"
        assert by_field["gross_amount"]["reconciliation"]["total_variance_pct"] == 3.4
        assert by_field["gross_amount"]["build"]["passed"] is True
        assert by_field["net_amount"]["outcome"] == "SELECTED"
        assert by_field["net_amount"]["reconciliation"]["total_variance_pct"] == 0.0
        assert by_field["net_amount"]["reconciliation"]["primary_key_overlap_pct"] == 100
        assert by_field["net_amount"]["reconciliation"]["current_row_count"] == 10
        assert by_field["net_amount"]["reconciliation"]["baseline_row_count"] == 10
        assert by_field["net_amount"]["build"]["passed_checks"] == 8
        assert case["pull_request"]["integration"]["status"] == "NOT_RUN"
        assert "customer_id" in case["patch"]["content"]
        assert 'env_var("DATARESCUE_REVENUE_COLUMN", "net_amount")' in case["patch"][
            "content"
        ]

        events = client.get(f"/api/v1/cases/{case['id']}/events")
        assert events.status_code == 200
        assert "event: CANDIDATE_ASSESSED" in events.text
        assert "event: PR_ATTEMPTED" in events.text


def test_schema_event_is_idempotent_and_reset_is_append_only(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post("/api/v1/demo/drift", json={}).json()
        raw_before = app.state.workflow.store.raw_event_count()
        second = client.post("/api/v1/demo/drift", json={}).json()
        assert second["deduplicated"] is True
        assert second["case"]["id"] == first["case"]["id"]
        assert app.state.workflow.store.raw_event_count() == raw_before

        reset = client.post("/api/v1/demo/reset")
        assert reset.status_code == 200
        assert client.get("/api/v1/cases").json() == []
        assert app.state.workflow.store.raw_event_count() == raw_before + 1

        replayed = client.post("/api/v1/demo/drift", json={}).json()
        assert replayed["deduplicated"] is False
        assert replayed["case"]["id"] == first["case"]["id"]


def test_asset_allowlist_rejects_unscoped_schema_events(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    payload = {
        "entity_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,other.raw.secrets,PROD)",
        "before_fields": [{"name": "amount"}],
        "after_fields": [{"name": "net_amount"}],
    }
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/events/schema-change", json=payload)

    assert response.status_code == 403
    assert "allowlist" in response.json()["detail"]


def test_connected_mode_requires_a_real_datahub_incident(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path, replay_mode=False)
    with TestClient(create_app(settings)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]

    assert case["state"] == "FAILED"
    assert case["incident_integration"]["status"] == "NOT_CONFIGURED"
    assert case["candidates"] == []
    assert case["pull_request"] is None

    result = cli_main(
        [
            "guard",
            "--asset",
            DEFAULT_ASSET_URN,
            "--database-path",
            str(settings.resolved_database_path),
            "--",
            "python",
            "-c",
            "raise SystemExit('must not run after a failed safety workflow')",
        ]
    )
    assert result == CONTAINED_EXIT_CODE


def test_fail_closed_scenario_activates_guard_exit_75(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/demo/drift", json={"scenario": "fail-closed"}
        )
        assert response.status_code == 200
        case = response.json()["case"]
        assert case["state"] == "CONTAINED"
        assert case["pull_request"] is None
        assert case["containment_reasons"]
        by_field = {item["source_field"]: item for item in case["candidates"]}
        assert by_field["settlement_amount"]["build"]["passed"] is True
        assert (
            by_field["settlement_amount"]["reconciliation"]["total_variance_pct"]
            == -1.5
        )
        assert by_field["settlement_amount"]["outcome"] == "REJECTED"

    result = cli_main(
        [
            "guard",
            "--asset",
            DEFAULT_ASSET_URN,
            "--database-path",
            str(settings.resolved_database_path),
            "--",
            "python",
            "-c",
            "raise SystemExit('must not run')",
        ]
    )
    assert result == CONTAINED_EXIT_CODE


class _SuccessfulDraftPR:
    def create_draft(self, *, case_id: str, patch: object) -> PullRequestArtifact:
        url = f"https://github.test/datarescue/pull/{case_id}"
        return PullRequestArtifact(
            branch=f"datarescue/{case_id.casefold()}",
            url=url,
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="test_create_draft_pr",
                message="test double opened a draft PR",
                resource_id=url,
            ),
        )


class _SuccessfulIncidents:
    def raise_incident(
        self, *, asset_urn: str, case_id: str, description: str
    ) -> IntegrationResult:
        del asset_urn, description
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="test_raise_incident",
            message="incident raised",
            resource_id=f"urn:li:incident:{case_id}",
        )

    def resolve_incident(self, *, incident_urn: str) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="test_resolve_incident",
            message="incident resolved",
            resource_id=incident_urn,
        )


class _FailingWritebackMCP:
    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        return MCPContextResult(
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="test_fetch_context",
                message="context fetched",
            ),
            context={
                "glossary_definition": (
                    "Recognized revenue is the net settled amount after processing fees."
                ),
                "owner": "Finance Data",
                "lineage_urns": [asset_urn],
                "context_documents": ["urn:li:glossaryTerm:NetRevenue"],
                "lineage_current": True,
            },
        )

    def write_evidence_report(self, **kwargs: object) -> IntegrationResult:
        del kwargs
        return IntegrationResult(
            status=IntegrationStatus.FAILED,
            operation="test_write_evidence",
            message="writeback failed",
        )


class _SuccessfulMCP(_FailingWritebackMCP):
    def write_evidence_report(self, **kwargs: object) -> IntegrationResult:
        del kwargs
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="test_write_evidence",
            message="evidence written",
        )


class _ConnectedCandidates:
    def propose(
        self, event: object, context: ContextBundle
    ) -> CandidateGenerationResult:
        del event
        evidence = context.context_documents
        return CandidateGenerationResult(
            candidates=[
                CandidateProposal(
                    id="gross",
                    source_field="gross_amount",
                    rationale="gross candidate",
                    semantic_evidence=evidence,
                ),
                CandidateProposal(
                    id="net",
                    source_field="net_amount",
                    rationale="net candidate",
                    semantic_evidence=evidence,
                ),
            ],
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="test_candidate_generation",
                message="candidates generated",
            ),
        )


def test_connected_mode_does_not_open_pr_when_evidence_writeback_fails(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(tmp_path, replay_mode=False)
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),  # type: ignore[arg-type]
        mcp=_FailingWritebackMCP(),  # type: ignore[arg-type]
        candidates=_ConnectedCandidates(),  # type: ignore[arg-type]
        executor=ReplayEvidenceExecutor(),
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]

    assert case["state"] == "FAILED"
    assert case["selected_candidate"]["source_field"] == "net_amount"
    assert case["evidence_writeback"]["status"] == "FAILED"
    assert case["pull_request"] is None
    assert "PR_OPENED" not in [event["event_type"] for event in case["events"]]


def test_incident_resolves_only_after_post_deploy_verification(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    workflow = WorkflowService(settings, github=_SuccessfulDraftPR())  # type: ignore[arg-type]
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        assert case["incident_status"] == "ACTIVE"

        verified = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={
                "merged_commit_sha": "abcdef1",
                "semantic_verdict": "MATCH",
                "lineage_current": True,
                "reconciliation": {
                    "total_variance_pct": 0.0,
                    "row_count_variance_pct": 0.0,
                    "primary_key_overlap_pct": 100.0,
                    "null_rate_delta_percentage_points": 0.0,
                },
                "build": {"passed": True, "passed_checks": 8, "total_checks": 8},
            },
        )

        assert verified.status_code == 200
        recovered = verified.json()
        assert recovered["state"] == "RESOLVED"
        assert recovered["incident_status"] == "RESOLVED"
        event_types = [event["event_type"] for event in recovered["events"]]
        assert event_types.index("PR_OPENED") < event_types.index("DEPLOYMENT_RECORDED")
        assert event_types.index("POST_DEPLOY_VERIFIED") < event_types.index("INCIDENT_RESOLVED")


class _CountingExecutor:
    produces_live_evidence = True

    def __init__(self) -> None:
        self.delegate = ReplayEvidenceExecutor()
        self.calls: list[str] = []
        self.verified_checkouts: list[Path] = []

    def bind_to_checkout(
        self, *, checkout_root: Path, repository_root: Path
    ) -> _CountingExecutor:
        del repository_root
        self.verified_checkouts.append(checkout_root)
        return self

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        self.calls.append(proposal.source_field)
        return self.delegate.execute(case_id=case_id, proposal=proposal, context=context)


def test_live_post_deploy_ignores_caller_evidence_and_recomputes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(tmp_path, execution_mode="postgres")
    executor = _CountingExecutor()
    workflow = WorkflowService(
        settings,
        mcp=_SuccessfulMCP(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
    )
    model_path = settings.github_repo_root / settings.github_patch_path
    merged_model = model_path.read_text(encoding="utf-8").replace(
        'env_var("DATARESCUE_REVENUE_COLUMN", "amount")',
        'env_var("DATARESCUE_REVENUE_COLUMN", "net_amount")',
    )

    def fake_git(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[1] == "show":
            stdout = merged_model
        elif command[1:4] == ["remote", "get-url", "origin"]:
            stdout = "git@github.com:girginomer10/datarescue.git\n"
        elif command[1] == "rev-parse":
            stdout = "a" * 40 + "\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        # Deliberately hostile/failed caller evidence must be ignored in live mode.
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={
                "merged_commit_sha": "abcdef1",
                "semantic_verdict": "CONFLICT",
                "lineage_current": False,
                "reconciliation": {
                    "total_variance_pct": 99.0,
                    "row_count_variance_pct": 99.0,
                    "primary_key_overlap_pct": 0.0,
                    "null_rate_delta_percentage_points": 99.0,
                },
                "build": {"passed": False, "passed_checks": 0, "total_checks": 8},
            },
        )

    assert response.status_code == 200
    recovered = response.json()
    assert recovered["state"] == "RESOLVED"
    assert executor.calls == ["gross_amount", "net_amount", "net_amount"]
    assert len(executor.verified_checkouts) == 1
    deployed = next(
        event for event in recovered["events"] if event["event_type"] == "DEPLOYMENT_RECORDED"
    )
    assert deployed["payload"]["evidence_mode"] == "LIVE_RECOMPUTED"
