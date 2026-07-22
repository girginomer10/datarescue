from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import apps.api.workflow as workflow_module
from apps.api.main import SQLITE_MAX_INTEGER, create_app
from apps.api.models import (
    CandidateProposal,
    ContextBundle,
    IntegrationResult,
    IntegrationStatus,
    PullRequestArtifact,
    SchemaChangeEvent,
    SchemaChangeResponse,
    SchemaField,
)
from apps.api.workflow import DEFAULT_ASSET_URN, WorkflowService
from packages.datahub.actions import DataHubSchemaMCLWatcher, HTTPEventSink, MCLActionStatus
from packages.datahub.mcp import MCPContextResult
from packages.evidence.executor import (
    CandidateExecution,
    PostgresDbtExecutor,
    ReplayEvidenceExecutor,
)
from tests.backend_helpers import REPO_ROOT, make_test_settings


def _schema_event(*, event_id: str | None = None, source: str = "DATAHUB_MCL") -> SchemaChangeEvent:
    return SchemaChangeEvent(
        event_id=event_id,
        entity_urn=DEFAULT_ASSET_URN,
        before_fields=[
            SchemaField(name="payment_id", data_type="bigint", nullable=False),
            SchemaField(name="amount", data_type="numeric", nullable=False),
        ],
        after_fields=[
            SchemaField(name="payment_id", data_type="bigint", nullable=False),
            SchemaField(name="gross_amount", data_type="numeric", nullable=False),
            SchemaField(name="net_amount", data_type="numeric", nullable=False),
        ],
        source=source,
    )


def _schema_payload(*, event_id: str, source: str = "DATAHUB_MCL") -> dict[str, object]:
    return {
        "event_id": event_id,
        "entity_urn": DEFAULT_ASSET_URN,
        "before_fields": [
            {"name": "payment_id", "data_type": "bigint", "nullable": False},
            {"name": "amount", "data_type": "numeric", "nullable": False},
        ],
        "after_fields": [
            {"name": "payment_id", "data_type": "bigint", "nullable": False},
            {"name": "gross_amount", "data_type": "numeric", "nullable": False},
            {"name": "net_amount", "data_type": "numeric", "nullable": False},
        ],
        "source": source,
    }


def _drift_mcl() -> dict[str, object]:
    def aspect(names: list[str]) -> dict[str, str]:
        return {
            "contentType": "application/json",
            "value": json.dumps({"fields": [{"fieldPath": name} for name in names]}),
        }

    return {
        "event_type": "MetadataChangeLogEvent_v1",
        "entityType": "dataset",
        "aspectName": "schemaMetadata",
        "entityUrn": DEFAULT_ASSET_URN,
        "previousAspectValue": aspect(["payment_id", "amount"]),
        "aspect": aspect(["payment_id", "gross_amount", "net_amount"]),
    }


def test_postgres_default_executor_requires_a_dsn_but_allows_injected_fakes(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(tmp_path, execution_mode="postgres", postgres_dsn=None)

    with pytest.raises(ValueError, match="POSTGRES_DSN"):
        WorkflowService(settings)

    class _InjectedExecutor:
        produces_live_evidence = True

        def execute(
            self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
        ) -> CandidateExecution:
            raise AssertionError("construction must not execute injected test doubles")

    workflow = WorkflowService(settings, executor=_InjectedExecutor())
    assert not isinstance(workflow.executor, ReplayEvidenceExecutor)


def test_postgres_default_executor_is_live_when_configured(tmp_path: Path) -> None:
    settings = make_test_settings(
        tmp_path,
        execution_mode="postgres",
        postgres_dsn="postgresql://datarescue@localhost/datarescue",
    )

    workflow = WorkflowService(settings)

    assert isinstance(workflow.executor, PostgresDbtExecutor)


class _SuccessfulIncidents:
    def __init__(self) -> None:
        self.resolved = False

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
        self.resolved = True
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="resolve_incident",
            message="resolved",
            resource_id=incident_urn,
        )


class _SuccessfulMCP:
    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        return MCPContextResult(
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="fetch_context",
                message="context fetched",
            ),
            context={
                "glossary_definition": (
                    "Recognized revenue is the net settled amount after processing fees."
                ),
                "owner": "Finance",
                "lineage_urns": [asset_urn],
                "context_documents": ["urn:li:glossaryTerm:NetRevenue"],
                "lineage_current": True,
            },
        )

    def write_evidence_report(self, **kwargs: object) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="mcp_write_evidence",
            message="evidence written",
        )


class _ReplayFallbackMCP(_SuccessfulMCP):
    def fetch_context(self, asset_urn: str) -> MCPContextResult:
        return MCPContextResult(
            integration=IntegrationResult(
                status=IntegrationStatus.FAILED,
                operation="fetch_context",
                message="live DataHub context unavailable",
                resource_id=asset_urn,
            ),
            context={},
        )


class _SuccessfulDraftPR:
    def create_draft(self, *, case_id: str, patch: object) -> PullRequestArtifact:
        url = f"https://github.test/datarescue/pull/{case_id}"
        return PullRequestArtifact(
            branch=f"datarescue/{case_id.casefold()}",
            url=url,
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="create_draft_pr",
                message="draft opened",
                resource_id=url,
            ),
        )


def _good_verification_payload() -> dict[str, object]:
    return {
        "merged_commit_sha": "abcdef1",
        "reconciliation": {
            "total_variance_pct": 0.0,
            "row_count_variance_pct": 0.0,
            "primary_key_overlap_pct": 100.0,
            "null_rate_delta_percentage_points": 0.0,
        },
        "build": {"passed": True, "passed_checks": 8, "total_checks": 8},
    }


def test_replay_executor_cannot_close_a_postgres_mode_live_incident(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path, execution_mode="postgres", postgres_dsn=None)
    incidents = _SuccessfulIncidents()
    workflow = WorkflowService(
        settings,
        incidents=incidents,
        mcp=_SuccessfulMCP(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=ReplayEvidenceExecutor(),
    )

    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PR_OPEN"
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json=_good_verification_payload(),
        )
        unchanged = client.get(f"/api/v1/cases/{case['id']}").json()

    assert response.status_code == 409
    assert "live evidence" in response.json()["detail"]
    assert unchanged["state"] == "PR_OPEN"
    assert "DEPLOYMENT_RECORDED" not in [event["event_type"] for event in unchanged["events"]]
    assert incidents.resolved is False


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_http_mcl_sink_does_not_skip_unlabelled_api_errors(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "authorization or server failure"})

    watcher = DataHubSchemaMCLWatcher(
        HTTPEventSink("http://api.test", transport=httpx.MockTransport(handler))
    )

    result = watcher.handle(_drift_mcl())

    assert result.status is MCLActionStatus.FAILED
    assert str(status_code) in result.reason


def test_http_mcl_sink_skips_only_machine_labelled_allowlist_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"detail": "Asset is outside the configured remediation allowlist"},
            headers={"X-DataRescue-Error": "ASSET_OUTSIDE_ALLOWLIST"},
        )

    watcher = DataHubSchemaMCLWatcher(
        HTTPEventSink("http://api.test", transport=httpx.MockTransport(handler))
    )

    result = watcher.handle(_drift_mcl())

    assert result.status is MCLActionStatus.SKIPPED
    assert "allowlist" in result.reason.lower()


def test_http_mcl_sink_does_not_trust_an_error_header_without_matching_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"detail": "invalid bearer token"},
            headers={"X-DataRescue-Error": "ASSET_OUTSIDE_ALLOWLIST"},
        )

    watcher = DataHubSchemaMCLWatcher(
        HTTPEventSink("http://api.test", transport=httpx.MockTransport(handler))
    )

    result = watcher.handle(_drift_mcl())

    assert result.status is MCLActionStatus.FAILED


def test_schema_allowlist_response_carries_machine_readable_reason(tmp_path: Path) -> None:
    payload = _schema_payload(event_id="outside-1")
    payload["entity_urn"] = "urn:li:dataset:(urn:li:dataPlatform:postgres,other.raw.x,PROD)"

    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.post("/api/v1/events/schema-change", json=payload)

    assert response.status_code == 403
    assert response.headers["x-datarescue-error"] == "ASSET_OUTSIDE_ALLOWLIST"


def test_case_id_prefix_collision_allocates_a_distinct_deterministic_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow_module, "case_id_from_dedup", lambda dedup_key: "DR-COLLIDE")
    settings = make_test_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/v1/demo/drift", json={}).json()
        second = client.post(
            "/api/v1/demo/drift", json={"scenario": "fail-closed"}
        ).json()
        cases = client.get("/api/v1/cases").json()

    assert first["case"]["id"] == "DR-COLLIDE"
    assert second["deduplicated"] is False
    assert second["case"]["id"] != first["case"]["id"]
    assert second["case"]["id"].startswith("DR-")
    assert len(cases) == 2


def test_canonical_case_id_remains_stable(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        case_id = client.post("/api/v1/demo/drift", json={}).json()["case"]["id"]

    assert case_id == "DR-996C48F0"


def test_reused_event_id_is_idempotent_only_for_the_same_content(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    payload = _schema_payload(event_id="mcl-shared-id")

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/v1/events/schema-change", json=payload)
        same = client.post("/api/v1/events/schema-change", json=payload)
        reordered = {
            **payload,
            "before_fields": list(reversed(payload["before_fields"])),  # type: ignore[arg-type]
            "after_fields": list(reversed(payload["after_fields"])),  # type: ignore[arg-type]
        }
        same_reordered = client.post("/api/v1/events/schema-change", json=reordered)
        changed_source = client.post(
            "/api/v1/events/schema-change",
            json={**payload, "source": "MANUAL_REPLAY"},
        )
        changed_schema_payload = _schema_payload(event_id="mcl-shared-id")
        after_fields = list(changed_schema_payload["after_fields"])  # type: ignore[arg-type]
        after_fields[-1] = {
            "name": "settlement_amount",
            "data_type": "numeric",
            "nullable": False,
        }
        changed_schema_payload["after_fields"] = after_fields
        changed_schema = client.post(
            "/api/v1/events/schema-change", json=changed_schema_payload
        )

    assert first.status_code == 202
    assert same.status_code == 202
    assert same.json()["deduplicated"] is True
    assert same_reordered.status_code == 202
    assert same_reordered.json()["deduplicated"] is True
    assert changed_source.status_code == 409
    assert changed_schema.status_code == 409


@pytest.mark.parametrize(
    "header",
    ["not-an-integer", "-1", str(SQLITE_MAX_INTEGER + 1), "9" * 10_000],
)
def test_invalid_or_overflowing_last_event_id_is_rejected(
    tmp_path: Path, header: str
) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        case_id = client.post("/api/v1/demo/drift", json={}).json()["case"]["id"]
        response = client.get(
            f"/api/v1/cases/{case_id}/events", headers={"Last-Event-ID": header}
        )

    assert response.status_code == 400


def test_overflowing_after_query_is_rejected_before_sqlite_binding(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        case_id = client.post("/api/v1/demo/drift", json={}).json()["case"]["id"]
        response = client.get(
            f"/api/v1/cases/{case_id}/events", params={"after": SQLITE_MAX_INTEGER + 1}
        )

    assert response.status_code == 400


class _BlockingExecutor:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.delegate = ReplayEvidenceExecutor()

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test executor was not released")
        return self.delegate.execute(case_id=case_id, proposal=proposal, context=context)


def test_duplicate_delivery_bypasses_a_long_running_worker_lock(tmp_path: Path) -> None:
    executor = _BlockingExecutor()
    workflow = WorkflowService(make_test_settings(tmp_path), executor=executor)
    event = _schema_event(event_id="fast-redelivery")
    first_done = threading.Event()
    duplicate_done = threading.Event()
    results: dict[str, SchemaChangeResponse] = {}

    def run_first() -> None:
        results["first"] = workflow.ingest(event)
        first_done.set()

    def run_duplicate() -> None:
        results["duplicate"] = workflow.ingest(event)
        duplicate_done.set()

    first = threading.Thread(target=run_first)
    duplicate = threading.Thread(target=run_duplicate)
    first.start()
    try:
        assert executor.entered.wait(2)
        duplicate.start()
        assert duplicate_done.wait(1), "duplicate delivery waited behind the dbt worker lock"
    finally:
        executor.release.set()
    first.join(5)
    duplicate.join(5)

    assert first_done.is_set()
    duplicate_response = results["duplicate"]
    assert duplicate_response.deduplicated is True


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _verified_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    origin = tmp_path / "origin.git"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "DataRescue Tests")
    _git(repository, "config", "user.email", "datarescue@example.test")
    model = repository / "demo/dbt/models/staging/stg_payments.sql"
    model.parent.mkdir(parents=True)
    model.write_text(
        (REPO_ROOT / "demo/dbt/models/staging/stg_payments.sql").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial model")
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "push", "-u", "origin", "main")
    return repository, model


class _CheckoutRecordingExecutor:
    produces_live_evidence = True

    def __init__(self) -> None:
        self.delegate = ReplayEvidenceExecutor()
        self.bound_checkouts: list[Path] = []
        self.bound_model_contents: list[str] = []

    def bind_to_checkout(
        self, *, checkout_root: Path, repository_root: Path
    ) -> _CheckoutRecordingExecutor:
        assert checkout_root != repository_root
        model = checkout_root / "demo/dbt/models/staging/stg_payments.sql"
        self.bound_checkouts.append(checkout_root)
        self.bound_model_contents.append(model.read_text(encoding="utf-8"))
        return self

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        return self.delegate.execute(case_id=case_id, proposal=proposal, context=context)


def _verified_workflow(
    tmp_path: Path, repository: Path, executor: _CheckoutRecordingExecutor
) -> tuple[object, WorkflowService]:
    settings = make_test_settings(
        tmp_path,
        execution_mode="postgres",
        postgres_dsn=None,
        github_repo_root=repository,
        dbt_project_dir=repository / "demo/dbt",
        dbt_profiles_dir=repository / "demo/dbt",
    )
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),
        mcp=_SuccessfulMCP(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=executor,
    )
    return settings, workflow


def _commit_and_push(repository: Path, model: Path, content: str, message: str) -> str:
    model.write_text(content, encoding="utf-8")
    _git(repository, "add", model.relative_to(repository).as_posix())
    _git(repository, "commit", "-m", message)
    _git(repository, "push", "origin", "main")
    return _git(repository, "rev-parse", "HEAD")


def test_post_deploy_executes_the_exact_origin_artifact_in_an_isolated_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, model = _verified_repository(tmp_path)
    monkeypatch.setattr(
        workflow_module,
        "_github_repository_from_remote_url",
        lambda url: "girginomer10/datarescue",
    )
    temporary_directory = workflow_module.tempfile.TemporaryDirectory
    cleanup_options: list[dict[str, object]] = []

    def recording_temporary_directory(
        *args: object, **kwargs: object
    ) -> tempfile.TemporaryDirectory[str]:
        cleanup_options.append(dict(kwargs))
        return temporary_directory(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        workflow_module.tempfile,
        "TemporaryDirectory",
        recording_temporary_directory,
    )
    executor = _CheckoutRecordingExecutor()
    settings, workflow = _verified_workflow(tmp_path, repository, executor)

    with TestClient(create_app(settings, workflow)) as client:  # type: ignore[arg-type]
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        patch_content = case["patch"]["content"]
        commit = _commit_and_push(
            repository, model, patch_content, "merge exact validated patch"
        )
        # A dirty and incorrect current checkout must be irrelevant to verification.
        model.write_text("-- current checkout must never execute\n", encoding="utf-8")
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={**_good_verification_payload(), "merged_commit_sha": commit[:7]},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "RESOLVED"
    assert response.json()["deployment_commit"] == commit
    assert executor.bound_model_contents == [patch_content]
    assert len(executor.bound_checkouts) == 1
    assert not executor.bound_checkouts[0].exists()
    assert cleanup_options == [
        {
            "prefix": f"verify-{case['id'].casefold()}-",
            "dir": settings.runtime_dir,
            "ignore_cleanup_errors": True,
        }
    ]


def test_comment_substring_and_correct_current_checkout_cannot_approve_wrong_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, model = _verified_repository(tmp_path)
    monkeypatch.setattr(
        workflow_module,
        "_github_repository_from_remote_url",
        lambda url: "girginomer10/datarescue",
    )
    executor = _CheckoutRecordingExecutor()
    settings, workflow = _verified_workflow(tmp_path, repository, executor)

    with TestClient(create_app(settings, workflow)) as client:  # type: ignore[arg-type]
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        patch_content = case["patch"]["content"]
        expected_substring = 'env_var("DATARESCUE_REVENUE_COLUMN", "net_amount")'
        wrong_artifact = f"-- dead text: {expected_substring}\n" + model.read_text(
            encoding="utf-8"
        )
        commit = _commit_and_push(
            repository, model, wrong_artifact, "merge wrong commented artifact"
        )
        # Recreate the old vulnerability: checkout looks right while SHA is wrong.
        model.write_text(patch_content, encoding="utf-8")
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={**_good_verification_payload(), "merged_commit_sha": commit},
        )
        unchanged = client.get(f"/api/v1/cases/{case['id']}").json()

    assert response.status_code == 409
    assert unchanged["state"] == "PR_OPEN"
    assert "DEPLOYMENT_RECORDED" not in [event["event_type"] for event in unchanged["events"]]
    assert executor.bound_checkouts == []


def test_commit_outside_fetched_origin_base_history_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, model = _verified_repository(tmp_path)
    monkeypatch.setattr(
        workflow_module,
        "_github_repository_from_remote_url",
        lambda url: "girginomer10/datarescue",
    )
    executor = _CheckoutRecordingExecutor()
    settings, workflow = _verified_workflow(tmp_path, repository, executor)

    with TestClient(create_app(settings, workflow)) as client:  # type: ignore[arg-type]
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        model.write_text(case["patch"]["content"], encoding="utf-8")
        _git(repository, "add", model.relative_to(repository).as_posix())
        _git(repository, "commit", "-m", "local unmerged patch")
        unmerged_commit = _git(repository, "rev-parse", "HEAD")
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={**_good_verification_payload(), "merged_commit_sha": unmerged_commit},
        )
        unchanged = client.get(f"/api/v1/cases/{case['id']}").json()

    assert response.status_code == 409
    assert unchanged["state"] == "PR_OPEN"
    assert executor.bound_checkouts == []


def test_missing_origin_base_is_rejected_before_artifact_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = _verified_repository(tmp_path)
    monkeypatch.setattr(
        workflow_module,
        "_github_repository_from_remote_url",
        lambda url: "girginomer10/datarescue",
    )
    executor = _CheckoutRecordingExecutor()
    settings, workflow = _verified_workflow(tmp_path, repository, executor)
    settings.github_base_branch = "missing-base"

    with TestClient(create_app(settings, workflow)) as client:  # type: ignore[arg-type]
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json=_good_verification_payload(),
        )
        unchanged = client.get(f"/api/v1/cases/{case['id']}").json()

    assert response.status_code == 409
    assert unchanged["state"] == "PR_OPEN"
    assert executor.bound_checkouts == []


def test_mismatched_origin_fetch_url_is_rejected_before_fetch_or_execution(
    tmp_path: Path,
) -> None:
    repository, _ = _verified_repository(tmp_path)
    _git(
        repository,
        "remote",
        "set-url",
        "origin",
        "git@github.com:attacker/other-repository.git",
    )
    executor = _CheckoutRecordingExecutor()
    settings, workflow = _verified_workflow(tmp_path, repository, executor)

    with TestClient(create_app(settings, workflow)) as client:  # type: ignore[arg-type]
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json=_good_verification_payload(),
        )
        unchanged = client.get(f"/api/v1/cases/{case['id']}").json()

    assert response.status_code == 409
    assert "origin fetch URL" in response.json()["detail"]
    assert unchanged["state"] == "PR_OPEN"
    assert executor.bound_checkouts == []


def test_replay_context_fallback_cannot_close_a_live_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, model = _verified_repository(tmp_path)
    monkeypatch.setattr(
        workflow_module,
        "_github_repository_from_remote_url",
        lambda url: "girginomer10/datarescue",
    )
    executor = _CheckoutRecordingExecutor()
    settings = make_test_settings(
        tmp_path,
        execution_mode="postgres",
        postgres_dsn=None,
        github_repo_root=repository,
        dbt_project_dir=repository / "demo/dbt",
        dbt_profiles_dir=repository / "demo/dbt",
    )
    workflow = WorkflowService(
        settings,
        incidents=_SuccessfulIncidents(),
        mcp=_ReplayFallbackMCP(),  # type: ignore[arg-type]
        github=_SuccessfulDraftPR(),  # type: ignore[arg-type]
        executor=executor,
    )

    with TestClient(create_app(settings, workflow)) as client:
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["incident_integration"]["status"] == "SUCCEEDED"
        commit = _commit_and_push(
            repository,
            model,
            case["patch"]["content"],
            "merge patch without fresh DataHub context",
        )
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={**_good_verification_payload(), "merged_commit_sha": commit},
        )
        unchanged = client.get(f"/api/v1/cases/{case['id']}").json()

    assert response.status_code == 409
    assert "Fresh live DataHub context" in response.json()["detail"]
    assert unchanged["state"] == "PR_OPEN"
    assert "DEPLOYMENT_RECORDED" not in [event["event_type"] for event in unchanged["events"]]
    assert executor.bound_checkouts == []


def test_postgres_executor_rebinds_dbt_paths_to_the_verified_checkout(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    checkout = tmp_path / "checkout"
    for root in (repository, checkout):
        (root / "demo/dbt").mkdir(parents=True)
    executor = PostgresDbtExecutor(
        postgres_dsn="postgresql://unused",
        dbt_project_dir=repository / "demo/dbt",
        dbt_profiles_dir=repository / "demo/dbt",
        evidence_dir=tmp_path / "evidence",
    )

    rebound = executor.bind_to_checkout(
        checkout_root=checkout, repository_root=repository
    )

    assert rebound.dbt_project_dir == (checkout / "demo/dbt").resolve()
    assert rebound.dbt_profiles_dir == (checkout / "demo/dbt").resolve()
    assert rebound.dbt_project_dir != executor.dbt_project_dir
    assert rebound.produces_live_evidence is True


def test_postgres_executor_refuses_a_project_outside_the_verified_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    checkout = tmp_path / "checkout"
    external_project = tmp_path / "external-dbt"
    repository.mkdir()
    checkout.mkdir()
    external_project.mkdir()
    executor = PostgresDbtExecutor(
        postgres_dsn="postgresql://unused",
        dbt_project_dir=external_project,
        dbt_profiles_dir=external_project,
    )

    with pytest.raises(ValueError, match="inside the configured git repository"):
        executor.bind_to_checkout(
            checkout_root=checkout, repository_root=repository
        )
