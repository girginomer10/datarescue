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
from packages.evidence.executor import CandidateExecution
from packages.policy import PolicyEngine
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
