"""Coverage for safety- and security-critical paths flagged by the audit.

These target branches that the vertical-slice tests never reach: the replay-mode
post-deploy reject path, per-gate policy boundaries, the shipped dbt-patch
injection guards, the asset scoping of the guard circuit breaker, the GitHub
adapter's failure and path-traversal guards, fail-closed behavior when the
evidence executor raises, and the DataHub Actions redelivery contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.cli import CONTAINED_EXIT_CODE
from apps.api.cli import main as cli_main
from apps.api.main import create_app
from apps.api.models import (
    BuildResult,
    CandidateProposal,
    ContextBundle,
    IntegrationResult,
    IntegrationStatus,
    PatchArtifact,
    PolicyConfig,
    PullRequestArtifact,
    ReconciliationMetrics,
    SemanticVerdict,
)
from apps.api.workflow import DEFAULT_ASSET_URN, WorkflowService
from packages.datahub.actions import DataHubSchemaMCLWatcher, DataRescueSchemaAction
from packages.datahub.mcp import MCPContextResult
from packages.evidence.executor import CandidateExecution, ReplayEvidenceExecutor
from packages.policy import PolicyEngine
from packages.remediation.candidates import CandidateGenerationResult
from packages.remediation.github import GitHubDraftPRAdapter
from packages.remediation.sql_safety import SQLSafetyError, render_dbt_patch
from tests.backend_helpers import REPO_ROOT, make_test_settings

STG_MODEL = REPO_ROOT / "demo/dbt/models/staging/stg_payments.sql"


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


def _hostile_verify_body() -> dict[str, object]:
    return {
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
    }


def test_replay_post_deploy_contains_on_failing_evidence(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    workflow = WorkflowService(settings, github=_SuccessfulDraftPR())  # type: ignore[arg-type]
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment", json=_hostile_verify_body()
        )
    assert response.status_code == 200
    recovered = response.json()
    assert recovered["state"] == "CONTAINED"
    assert recovered["incident_status"] == "ACTIVE"
    assert recovered["containment_reasons"]
    event_types = [event["event_type"] for event in recovered["events"]]
    assert "INCIDENT_RESOLVED" not in event_types


@pytest.mark.parametrize(
    ("override", "failing_gate"),
    [
        ({"total_variance_pct": 0.51}, "total_variance"),
        ({"row_count_variance_pct": 0.11}, "row_count_variance"),
        ({"primary_key_overlap_pct": 99.89}, "primary_key_overlap"),
        ({"null_rate_delta_percentage_points": 0.51}, "null_rate_delta"),
    ],
)
def test_policy_rejects_one_step_past_each_threshold(
    override: dict[str, float], failing_gate: str
) -> None:
    metrics = {
        "total_variance_pct": 0.0,
        "row_count_variance_pct": 0.0,
        "primary_key_overlap_pct": 100.0,
        "null_rate_delta_percentage_points": 0.0,
    }
    metrics.update(override)
    decision = PolicyEngine().evaluate(
        semantic_verdict=SemanticVerdict.MATCH,
        metrics=ReconciliationMetrics(**metrics),
        build=BuildResult(passed=True, passed_checks=8, total_checks=8),
        lineage_current=True,
    )
    assert decision.accepted is False
    assert {check.name for check in decision.checks if not check.passed} == {failing_gate}


def test_default_config_rejects_a_missing_verdict_in_isolation() -> None:
    safe = ReconciliationMetrics(
        total_variance_pct=0.0,
        row_count_variance_pct=0.0,
        primary_key_overlap_pct=100.0,
        null_rate_delta_percentage_points=0.0,
    )
    decision = PolicyEngine().evaluate(
        semantic_verdict=SemanticVerdict.MISSING,
        metrics=safe,
        build=BuildResult(passed=True, passed_checks=8, total_checks=8),
        lineage_current=True,
    )
    assert decision.accepted is False
    assert {check.name for check in decision.checks if not check.passed} == {"semantic_evidence"}


def test_relaxed_semantic_config_tolerates_missing_but_blocks_conflict() -> None:
    engine = PolicyEngine(PolicyConfig(semantic_evidence_required=False))
    safe = ReconciliationMetrics(
        total_variance_pct=0.0,
        row_count_variance_pct=0.0,
        primary_key_overlap_pct=100.0,
        null_rate_delta_percentage_points=0.0,
    )
    build = BuildResult(passed=True, passed_checks=8, total_checks=8)
    assert engine.evaluate(
        semantic_verdict=SemanticVerdict.MISSING, metrics=safe, build=build, lineage_current=True
    ).accepted is True
    assert engine.evaluate(
        semantic_verdict=SemanticVerdict.CONFLICT, metrics=safe, build=build, lineage_current=True
    ).accepted is False


@pytest.mark.parametrize(
    ("source_field", "target_alias"),
    [
        ("net_amount; DROP TABLE payments_raw", "revenue"),
        ("amount--", "revenue"),
        ("net_amount", "attacker_controlled"),
    ],
)
def test_render_dbt_patch_rejects_injection_and_non_revenue_alias(
    source_field: str, target_alias: str
) -> None:
    model = STG_MODEL.read_text(encoding="utf-8")
    proposal = CandidateProposal(
        id="hostile", source_field=source_field, target_alias=target_alias, rationale="r"
    )
    with pytest.raises(SQLSafetyError):
        render_dbt_patch(proposal, existing_sql=model)


def test_render_dbt_patch_rejects_ambiguous_or_missing_default() -> None:
    model = STG_MODEL.read_text(encoding="utf-8")
    net = CandidateProposal(id="net", source_field="net_amount", rationale="r")
    ambiguous = (
        model + '\n{% set revenue_column = env_var("DATARESCUE_REVENUE_COLUMN", "amount") %}'
    )
    with pytest.raises(SQLSafetyError):
        render_dbt_patch(net, existing_sql=ambiguous)
    missing = model.replace(
        'env_var("DATARESCUE_REVENUE_COLUMN", "amount")',
        'env_var("DATARESCUE_REVENUE_COLUMN", "customer_id")',
    )
    with pytest.raises(SQLSafetyError):
        render_dbt_patch(net, existing_sql=missing)


def test_guard_is_asset_scoped_not_a_global_block(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        case = client.post("/api/v1/demo/drift", json={"scenario": "fail-closed"}).json()["case"]
        assert case["state"] == "CONTAINED"
    db = str(settings.resolved_database_path)

    # A different, uncontained asset must still run its command.
    unrelated = cli_main(
        ["guard", "--asset", "urn:li:dataset:other", "--database-path", db,
         "--", sys.executable, "-c", "import sys; sys.exit(0)"]
    )
    assert unrelated == 0
    # The contained asset stays blocked.
    blocked = cli_main(
        ["guard", "--asset", DEFAULT_ASSET_URN, "--database-path", db,
         "--", sys.executable, "-c", "import sys; sys.exit(0)"]
    )
    assert blocked == CONTAINED_EXIT_CODE


class _RaisingExecutor:
    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        raise RuntimeError("postgres unavailable")


def test_case_id_prefix_collision_is_handled_as_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apps.api.workflow as workflow_module

    settings = make_test_settings(tmp_path)
    workflow = WorkflowService(settings)
    # Force two distinct drifts to collide on the same derived case id.
    monkeypatch.setattr(workflow_module, "case_id_from_dedup", lambda dedup_key: "DR-COLLIDE")
    with TestClient(create_app(settings, workflow)) as client:
        first = client.post("/api/v1/demo/drift", json={}).json()
        assert first["case"]["state"] == "PATCH_READY"  # advanced past DETECTED
        # A different drift (different dedup key) that maps to the same case id
        # must not escape as a 500.
        response = client.post("/api/v1/demo/drift", json={"scenario": "fail-closed"})
    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is True
    assert body["case"]["id"] == "DR-COLLIDE"


def test_executor_failure_fails_closed_to_rejected(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    workflow = WorkflowService(settings, executor=_RaisingExecutor())  # type: ignore[arg-type]
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
    assert case["state"] == "CONTAINED"
    assert case["candidates"]
    for candidate in case["candidates"]:
        assert candidate["outcome"] == "REJECTED"
        assert candidate["reconciliation"]["total_variance_pct"] == 1_000_000_000.0
        assert candidate["build"]["passed"] is False


def _patch() -> PatchArtifact:
    return PatchArtifact(
        path="x.sql", content="SELECT payment_id FROM raw.payments_raw", sha256="ab"
    )


def test_enabled_github_adapter_falls_back_to_bundle_without_a_git_repo(tmp_path: Path) -> None:
    adapter = GitHubDraftPRAdapter(
        enabled=True,
        repository="girginomer10/datarescue",
        repo_root=tmp_path,  # no .git here
        base_branch="main",
        patch_path="demo/dbt/models/staging/stg_payments.sql",
        runtime_dir=tmp_path / "runtime",
    )
    result = adapter.create_draft(case_id="DR-X", patch=_patch())
    assert result.integration.status is IntegrationStatus.FAILED
    assert result.bundle_path is not None
    assert Path(result.bundle_path).exists()
    # The persisted message must not leak resolved filesystem paths.
    assert str(tmp_path) not in result.integration.message


def test_enabled_github_adapter_rejects_path_traversal_patch_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "init", "-b", "main"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "i"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    # Pretend gh/git binaries exist so the patch-path guard is reached.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    adapter = GitHubDraftPRAdapter(
        enabled=True,
        repository="x/y",
        repo_root=repo,
        base_branch="main",
        patch_path="../../etc/evil",
        runtime_dir=tmp_path / "runtime",
    )
    result = adapter.create_draft(case_id="DR-X", patch=_patch())
    assert result.integration.status is IntegrationStatus.FAILED


def test_disabled_github_adapter_writes_a_faithful_bundle(tmp_path: Path) -> None:
    adapter = GitHubDraftPRAdapter(
        enabled=False,
        repository="girginomer10/datarescue",
        repo_root=tmp_path,
        base_branch="main",
        patch_path="demo/dbt/models/staging/stg_payments.sql",
        runtime_dir=tmp_path / "runtime",
    )
    patch = PatchArtifact(
        path="stg.sql",
        content="SELECT payment_id, net_amount AS revenue FROM raw.payments_raw",
        sha256="deadbeef",
    )
    result = adapter.create_draft(case_id="DR-996C48F0", patch=patch)
    assert result.integration.status is IntegrationStatus.NOT_RUN
    assert result.bundle_path is not None
    manifest = json.loads(Path(result.bundle_path).read_text(encoding="utf-8"))
    assert manifest["status"] == "NOT_RUN"
    assert manifest["patch_sha256"] == "deadbeef"
    assert manifest["branch"] == "datarescue/dr-996c48f0"
    sql_file = Path(result.bundle_path).parent / "payments_fct.sql"
    assert sql_file.read_text(encoding="utf-8") == patch.content


def test_datahub_action_create_validates_config() -> None:
    with pytest.raises(ValueError):
        DataRescueSchemaAction.create({})
    with pytest.raises(ValueError):
        DataRescueSchemaAction.create({"api_url": "http://x", "request_timeout_seconds": 600})
    action = DataRescueSchemaAction.create({"api_url": "http://x", "request_timeout_seconds": 5})
    assert isinstance(action, DataRescueSchemaAction)


# --------------------------------------------------------------------------- #
# Incident / post-deploy honesty and connected-mode containment
# --------------------------------------------------------------------------- #

_GLOSSARY = "Recognized revenue is the net settled amount after processing fees."


class _SuccessfulIncidents:
    def raise_incident(
        self, *, asset_urn: str, case_id: str, description: str
    ) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="raise_incident",
            message="raised",
            resource_id=f"urn:li:incident:{case_id}",
        )

    def resolve_incident(self, *, incident_urn: str) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="resolve_incident",
            message="resolved",
            resource_id=incident_urn,
        )


class _ResolveFailsIncidents(_SuccessfulIncidents):
    def resolve_incident(self, *, incident_urn: str) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.FAILED,
            operation="resolve_incident",
            message="incident resolution refused",
            resource_id=incident_urn,
        )


def _context_result(glossary: str | None = _GLOSSARY) -> MCPContextResult:
    return MCPContextResult(
        integration=IntegrationResult(
            status=IntegrationStatus.SUCCEEDED, operation="fetch_context", message="ctx"
        ),
        context={
            "glossary_definition": glossary,
            "owner": "Finance",
            "lineage_urns": [DEFAULT_ASSET_URN],
            "context_documents": ["urn:li:glossaryTerm:NetRevenue"],
            "lineage_current": True,
        },
    )


class _ValidContextMCP:
    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        return _context_result()

    def write_evidence_report(self, **kwargs: object) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED, operation="mcp_write_evidence", message="ok"
        )


class _NoGlossaryMCP(_ValidContextMCP):
    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        return _context_result(glossary=None)


class _WritebackFailsOnRecovered(_ValidContextMCP):
    def write_evidence_report(
        self, *, asset_urn: str, case_id: str, report: dict[str, object], degraded: bool
    ) -> IntegrationResult:
        if report.get("decision") == "RECOVERED":
            return IntegrationResult(
                status=IntegrationStatus.FAILED,
                operation="mcp_write_evidence",
                message="final write-back failed",
            )
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED, operation="mcp_write_evidence", message="ok"
        )


class _EmptyCandidates:
    def propose(self, event: object, context: object) -> CandidateGenerationResult:
        return CandidateGenerationResult(
            candidates=[],
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="candidate_generation",
                message="no candidate",
            ),
        )


def _good_verify_body() -> dict[str, object]:
    return {
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
    }


def _patched_model() -> str:
    return STG_MODEL.read_text(encoding="utf-8").replace(
        'env_var("DATARESCUE_REVENUE_COLUMN", "amount")',
        'env_var("DATARESCUE_REVENUE_COLUMN", "net_amount")',
    )


def _fake_git_show(model: str):  # type: ignore[no-untyped-def]
    def fake_git(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = model if command[1] == "show" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return fake_git


def test_replay_mode_refuses_to_resolve_a_live_incident(tmp_path: Path) -> None:
    # A live (SUCCEEDED) DataHub incident must not be resolved on caller evidence;
    # only postgres mode, which recomputes from the merge, may resolve it.
    settings = make_test_settings(tmp_path)
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),  # type: ignore[arg-type]
        mcp=_ValidContextMCP(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment", json=_good_verify_body()
        )
        assert response.status_code == 409
        assert client.get(f"/api/v1/cases/{case['id']}").json()["state"] == "PR_OPEN"


def test_ingest_records_a_durable_non_leaking_failed_event(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    model_dir = repo / "demo/dbt/models/staging"
    model_dir.mkdir(parents=True)
    # No allowlisted amount default, so render_dbt_patch raises inside _advance.
    (model_dir / "stg_payments.sql").write_text(
        "select payment_id, amount as revenue from x", encoding="utf-8"
    )
    settings = make_test_settings(tmp_path, github_repo_root=repo)
    with TestClient(create_app(settings)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
    assert case["state"] == "FAILED"
    failed = next(event for event in case["events"] if event["event_type"] == "FAILED")
    assert failed["payload"]["error_type"]
    assert "inspect server logs" in failed["payload"]["message"]
    assert str(repo) not in failed["payload"]["message"]


def test_post_deploy_writeback_failure_keeps_incident_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Postgres mode recomputes from the (faked) merge, passes the gates, then the
    # final RECOVERED write-back fails: the case must go FAILED, incident ACTIVE.
    settings = make_test_settings(tmp_path, execution_mode="postgres")
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),  # type: ignore[arg-type]
        mcp=_WritebackFailsOnRecovered(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=ReplayEvidenceExecutor(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(subprocess, "run", _fake_git_show(_patched_model()))
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        recovered = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment", json=_good_verify_body()
        ).json()
    assert recovered["state"] == "FAILED"
    assert recovered["incident_status"] == "ACTIVE"
    assert "INCIDENT_RESOLVED" not in [event["event_type"] for event in recovered["events"]]


def test_post_deploy_resolution_failure_keeps_incident_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(tmp_path, execution_mode="postgres")
    workflow = WorkflowService(
        settings,
        incidents=_ResolveFailsIncidents(),  # type: ignore[arg-type]
        mcp=_ValidContextMCP(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=ReplayEvidenceExecutor(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(subprocess, "run", _fake_git_show(_patched_model()))
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        recovered = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment", json=_good_verify_body()
        ).json()
    assert recovered["state"] == "FAILED"
    assert recovered["incident_status"] == "ACTIVE"


def test_connected_mode_contains_when_glossary_is_missing(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path, replay_mode=False)
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),  # type: ignore[arg-type]
        mcp=_NoGlossaryMCP(),  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
    assert case["state"] == "CONTAINED"
    assert case["containment_reasons"]


def test_connected_mode_contains_when_no_candidate_exists(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path, replay_mode=False)
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),  # type: ignore[arg-type]
        mcp=_ValidContextMCP(),  # type: ignore[arg-type]
        candidates=_EmptyCandidates(),  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
    assert case["state"] == "CONTAINED"
    assert any("candidate" in reason.lower() for reason in case["containment_reasons"])


def test_live_post_deploy_rejects_a_merge_without_the_validated_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(tmp_path, execution_mode="postgres")
    workflow = WorkflowService(
        settings,
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=ReplayEvidenceExecutor(),  # type: ignore[arg-type]
    )
    # The merged model as returned by `git show` still has the pre-drift default,
    # i.e. it does NOT contain the validated net_amount mapping.
    unpatched = (settings.github_repo_root / settings.github_patch_path).read_text(
        encoding="utf-8"
    )

    def fake_git(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = unpatched if command[1] == "show" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment", json=_good_verify_body()
        )
    assert response.status_code == 409
    assert client.get(f"/api/v1/cases/{case['id']}").json()["state"] == "PR_OPEN"


def test_datahub_action_raises_on_malformed_event_for_redelivery() -> None:
    action = DataRescueSchemaAction("http://api.test")
    captured: list[object] = []
    action.watcher = DataHubSchemaMCLWatcher(captured.append)
    malformed = {
        "entityType": "dataset",
        "aspectName": "schemaMetadata",
        "entityUrn": "urn:li:dataset:x",
        "previousAspectValue": {"contentType": "application/json", "value": "{}"},
        "aspect": {"contentType": "application/json", "value": json.dumps({"no_fields": []})},
    }

    class _Envelope:
        event = malformed

    with pytest.raises(ValueError):
        action.act(_Envelope())
    assert captured == []
