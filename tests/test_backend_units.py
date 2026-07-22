"""Focused unit coverage for safety-critical building blocks.

These tests exercise the deterministic primitives the higher-level workflow
depends on: the state machine, the request contracts, the SQL allowlist,
reconciliation math, the honest DataHub adapters, the MCL helpers, and the
guard circuit breaker. They deliberately avoid the network and Docker so they
run in the fast unit lane while still proving the fail-closed invariants.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from apps.api.cli import CONTAINED_EXIT_CODE
from apps.api.cli import main as cli_main
from apps.api.models import (
    BuildResult,
    CandidateProposal,
    CaseState,
    ContextBundle,
    IntegrationResult,
    IntegrationStatus,
    ReconciliationMetrics,
    SchemaChangeEvent,
    SchemaField,
    SemanticVerdict,
    VerifyDeploymentRequest,
)
from apps.api.state_machine import ALLOWED_TRANSITIONS, InvalidStateTransition, validate_transition
from apps.api.workflow import DEFAULT_ASSET_URN
from packages.datahub.actions import (
    DataHubSchemaMCLWatcher,
    HTTPEventSink,
    MCLActionStatus,
    _event_payload,
    _observed_at,
    _same_schema,
)
from packages.datahub.graphql import DataHubGraphQLAdapter, _find_urn
from packages.datahub.mcp import DataHubMCPAdapter
from packages.evidence.executor import (
    ReplayEvidenceExecutor,
    _candidate_schema,
    _percentage_delta,
    _semantic_verdict,
)
from packages.remediation.sql_safety import (
    IDENTIFIER,
    SQLSafetyError,
    render_candidate_sql,
    require_relation,
    validate_candidate_sql,
)
from tests.backend_helpers import make_test_settings

# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

NON_TERMINAL_STATES = [
    state for state, targets in ALLOWED_TRANSITIONS.items() if targets
]
TERMINAL_STATES = [state for state, targets in ALLOWED_TRANSITIONS.items() if not targets]


def test_new_case_must_start_in_detected() -> None:
    validate_transition(None, CaseState.DETECTED)
    with pytest.raises(InvalidStateTransition):
        validate_transition(None, CaseState.CONTEXT_GATHERED)


def test_same_state_is_a_legal_noop() -> None:
    # The workflow appends several events while staying in one state.
    validate_transition(CaseState.VALIDATING, CaseState.VALIDATING)
    validate_transition(CaseState.PATCH_READY, CaseState.PATCH_READY)


def test_illegal_forward_transition_is_rejected() -> None:
    with pytest.raises(InvalidStateTransition):
        validate_transition(CaseState.DETECTED, CaseState.PR_OPEN)


@pytest.mark.parametrize("terminal", TERMINAL_STATES)
def test_terminal_states_have_no_outgoing_transitions(terminal: CaseState) -> None:
    assert ALLOWED_TRANSITIONS[terminal] == set()
    with pytest.raises(InvalidStateTransition):
        validate_transition(terminal, CaseState.DETECTED)


@pytest.mark.parametrize("state", NON_TERMINAL_STATES)
def test_fail_closed_branches_are_reachable_from_every_active_state(
    state: CaseState,
) -> None:
    # Fail-closed exits must always be legal, whatever the pipeline stage.
    validate_transition(state, CaseState.CONTAINED)
    validate_transition(state, CaseState.FAILED)


def test_every_allowed_forward_transition_is_accepted() -> None:
    # Deleting a legal pipeline edge would silently break the workflow.
    for current, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CaseState.DEPLOYED, CaseState.VALIDATING),
        (CaseState.POST_DEPLOY_VERIFIED, CaseState.PATCH_READY),
        (CaseState.PATCH_READY, CaseState.DETECTED),
    ],
)
def test_backward_and_skip_transitions_are_rejected(
    current: CaseState, target: CaseState
) -> None:
    with pytest.raises(InvalidStateTransition):
        validate_transition(current, target)


def test_dedup_key_is_stable_across_field_order_and_urn_case() -> None:
    from apps.api.workflow import schema_event_dedup_key

    a = SchemaChangeEvent(
        entity_urn="URN:LI:X",
        before_fields=[SchemaField(name="a"), SchemaField(name="b")],
        after_fields=[SchemaField(name="c")],
    )
    b = SchemaChangeEvent(
        entity_urn="urn:li:x",
        before_fields=[SchemaField(name="b"), SchemaField(name="a")],
        after_fields=[SchemaField(name="c")],
    )
    assert schema_event_dedup_key(a) == schema_event_dedup_key(b)


# --------------------------------------------------------------------------- #
# Request contracts
# --------------------------------------------------------------------------- #


def _reconciliation() -> ReconciliationMetrics:
    return ReconciliationMetrics(
        total_variance_pct=0.0,
        row_count_variance_pct=0.0,
        primary_key_overlap_pct=100.0,
        null_rate_delta_percentage_points=0.0,
    )


def test_verify_request_rejects_passed_exceeding_total_checks() -> None:
    with pytest.raises(ValidationError):
        VerifyDeploymentRequest(
            merged_commit_sha="abcdef1",
            reconciliation=_reconciliation(),
            build=BuildResult(passed=True, passed_checks=9, total_checks=8),
        )


def test_verify_request_rejects_non_hex_commit_sha() -> None:
    with pytest.raises(ValidationError):
        VerifyDeploymentRequest(
            merged_commit_sha="not-a-sha",
            reconciliation=_reconciliation(),
            build=BuildResult(passed=True, passed_checks=8, total_checks=8),
        )


def test_verify_request_accepts_a_valid_payload() -> None:
    request = VerifyDeploymentRequest(
        merged_commit_sha="ABCDEF0123456789",
        reconciliation=_reconciliation(),
        build=BuildResult(passed=True, passed_checks=8, total_checks=8),
    )
    assert request.semantic_verdict is SemanticVerdict.MATCH


# --------------------------------------------------------------------------- #
# SQL allowlist
# --------------------------------------------------------------------------- #


def test_validate_candidate_sql_accepts_the_rendered_shape() -> None:
    proposal = CandidateProposal(id="c", source_field="net_amount", rationale="ok")
    rendered = render_candidate_sql(proposal, relation="payments_raw")
    assert validate_candidate_sql(rendered, relation="payments_raw") == rendered


@pytest.mark.parametrize(
    "bad_sql",
    [
        "SELECT payment_id, net_amount AS revenue FROM payments_raw; DROP TABLE payments_raw",
        "SELECT payment_id, net_amount AS revenue FROM payments_raw -- comment",
        "SELECT payment_id, net_amount AS revenue /* c */ FROM payments_raw",
        "SELECT payment_id, net_amount AS revenue, gross_amount FROM payments_raw",
        "SELECT payment_id, net_amount AS gross FROM payments_raw",
        "DELETE FROM payments_raw",
    ],
)
def test_validate_candidate_sql_rejects_anything_but_the_allowlisted_shape(
    bad_sql: str,
) -> None:
    with pytest.raises(SQLSafetyError):
        validate_candidate_sql(bad_sql, relation="payments_raw")


def test_require_relation_bounds_dotted_parts() -> None:
    assert require_relation("raw.payments_raw") == "raw.payments_raw"
    assert require_relation("db.raw.payments_raw") == "db.raw.payments_raw"
    with pytest.raises(SQLSafetyError):
        require_relation("a.b.c.d")
    with pytest.raises(SQLSafetyError):
        require_relation("raw.payments raw")


# --------------------------------------------------------------------------- #
# Reconciliation math and executor helpers
# --------------------------------------------------------------------------- #


def test_percentage_delta_handles_zero_baseline_without_dividing() -> None:
    assert _percentage_delta(0.0, 0.0) == 0.0
    assert _percentage_delta(5.0, 0.0) == 1_000_000_000.0
    assert _percentage_delta(103.4, 100.0) == pytest.approx(3.4)
    assert _percentage_delta(98.5, 100.0) == pytest.approx(-1.5)


def test_candidate_schema_is_a_safe_lowercase_identifier() -> None:
    schema = _candidate_schema("DR-996C48F0", "net_amount")
    assert schema == "candidate_dr_996c48f0_net_amount"
    assert IDENTIFIER.fullmatch(schema)


def _context(definition: str | None) -> ContextBundle:
    return ContextBundle(
        asset_urn="urn:li:dataset:test",
        glossary_definition=definition,
        source="test",
        integration=IntegrationResult(
            status=IntegrationStatus.SUCCEEDED, operation="t", message="t"
        ),
    )


def test_semantic_verdict_classifies_net_gross_and_missing() -> None:
    ctx = _context("Recognized revenue is the net settled amount after processing fees.")
    assert _semantic_verdict("net_amount", ctx) is SemanticVerdict.MATCH
    assert _semantic_verdict("gross_amount", ctx) is SemanticVerdict.CONFLICT
    assert _semantic_verdict("settlement_amount", ctx) is SemanticVerdict.MISSING
    assert _semantic_verdict("net_amount", _context(None)) is SemanticVerdict.MISSING


def test_replay_executor_fails_closed_for_unrecognized_candidate() -> None:
    execution = ReplayEvidenceExecutor().execute(
        case_id="DR-1",
        proposal=CandidateProposal(id="x", source_field="mystery_amount", rationale="r"),
        context=_context("net settled revenue"),
    )
    assert execution.build.passed is False
    assert execution.reconciliation.total_variance_pct == 1_000_000_000.0
    assert execution.reconciliation.primary_key_overlap_pct == 0.0


def test_run_dbt_never_reports_a_prior_candidates_stale_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from packages.evidence.executor import PostgresDbtExecutor

    project = tmp_path / "dbt"
    (project / "target").mkdir(parents=True)
    stale = project / "target" / "run_results.json"
    stale.write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "test.prior", "status": "pass"},
                    {"unique_id": "model.prior", "status": "success"},
                ]
            }
        ),
        encoding="utf-8",
    )
    executor = PostgresDbtExecutor(
        postgres_dsn="postgresql://unused",
        dbt_project_dir=project,
        dbt_profiles_dir=project,
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        # A compile/parse failure exits non-zero WITHOUT rewriting run_results.json.
        return subprocess.CompletedProcess([], 1, stdout="", stderr="compile error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = executor._run_dbt(schema="candidate_x", source_field="net_amount")

    assert result.passed is False
    assert result.passed_checks == 0
    assert result.total_checks == 0


# --------------------------------------------------------------------------- #
# DataHub MCP adapter (mocked transport, no network)
# --------------------------------------------------------------------------- #


def _mcp_handshake(handler_for_call):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"result": {}}, headers={"mcp-session-id": "s1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        return handler_for_call(body)

    return handler


def test_mcp_fetch_context_success_via_structured_content() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "structuredContent": {
                        "glossary_definition": "net settled revenue",
                        "lineage_current": True,
                    }
                }
            },
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context("urn:li:dataset:test")
    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert result.context["glossary_definition"] == "net settled revenue"
    assert result.context["lineage_current"] is True


def test_mcp_parses_event_stream_responses() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        stream = (
            'event: message\n'
            'data: {"jsonrpc":"2.0","id":2,"result":'
            '{"structuredContent":{"owner":"Finance Data"}}}\n\n'
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context("urn:li:dataset:test")
    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert result.context["owner"] == "Finance Data"


def test_mcp_tool_error_is_reported_as_failed_not_success() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}},
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context("urn:li:dataset:test")
    assert result.integration.status is IntegrationStatus.FAILED
    assert result.context == {}


def test_mcp_write_evidence_tags_degraded_and_returns_written_urn() -> None:
    captured: list[dict[str, object]] = []

    def call(body: dict[str, object]) -> httpx.Response:
        captured.append(body["params"]["arguments"])  # type: ignore[index]
        return httpx.Response(
            200, json={"result": {"structuredContent": {"urn": "urn:li:document:1"}}}
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.write_evidence_report(
        asset_urn="urn:li:dataset:test",
        case_id="DR-1",
        report={"decision": "AUTO_REPAIR_REFUSED"},
        degraded=True,
    )
    assert result.status is IntegrationStatus.SUCCEEDED
    assert result.resource_id == "urn:li:document:1"
    assert captured[0]["tags"] == ["datarescue:degraded"]


def test_mcp_parses_content_text_json_and_degrades_prose() -> None:
    def json_text(body: dict[str, object]) -> httpx.Response:
        payload = json.dumps({"owner": "Finance"})
        return httpx.Response(
            200,
            json={"result": {"content": [{"type": "text", "text": payload}]}},
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(_mcp_handshake(json_text))
    )
    assert adapter.fetch_context("urn:x").context == {"owner": "Finance"}

    def prose_text(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200, json={"result": {"content": [{"type": "text", "text": "just prose"}]}}
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(_mcp_handshake(prose_text))
    )
    assert adapter.fetch_context("urn:x").context == {"text": "just prose"}


def test_mcp_event_stream_returns_the_last_message() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        stream = (
            'data: {"jsonrpc":"2.0","method":"progress"}\n\n'
            'data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"owner":"Last"}}}\n\n'
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(_mcp_handshake(call))
    )
    assert adapter.fetch_context("urn:x").context == {"owner": "Last"}


def test_mcp_empty_event_stream_is_failed() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200, text="event: ping\n\n", headers={"content-type": "text/event-stream"}
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(_mcp_handshake(call))
    )
    assert adapter.fetch_context("urn:x").integration.status is IntegrationStatus.FAILED


def test_mcp_initialize_error_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"error": {"message": "handshake refused"}})
        return httpx.Response(202)

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(handler)
    )
    assert adapter.fetch_context("urn:x").integration.status is IntegrationStatus.FAILED


# --------------------------------------------------------------------------- #
# DataHub GraphQL adapter
# --------------------------------------------------------------------------- #


def test_find_urn_extracts_from_nested_structures() -> None:
    assert _find_urn("urn:li:incident:1") == "urn:li:incident:1"
    assert _find_urn({"incidentUrn": "urn:li:incident:2"}) == "urn:li:incident:2"
    assert _find_urn({"a": {"b": ["urn:li:incident:3"]}}) == "urn:li:incident:3"
    assert _find_urn({"value": "not-a-urn"}) is None
    assert _find_urn(None) is None


def test_resolve_incident_treats_unconfirmed_status_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"updateIncidentStatus": False}})

    adapter = DataHubGraphQLAdapter(
        gms_url="https://datahub.test", transport=httpx.MockTransport(handler)
    )
    result = adapter.resolve_incident(incident_urn="urn:li:incident:1")
    assert result.status is IntegrationStatus.FAILED
    assert result.resource_id == "urn:li:incident:1"


def test_raise_incident_without_urn_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"raiseIncident": None}})

    adapter = DataHubGraphQLAdapter(
        gms_url="https://datahub.test", transport=httpx.MockTransport(handler)
    )
    result = adapter.raise_incident(
        asset_urn="urn:li:dataset:test", case_id="DR-1", description="drift"
    )
    assert result.status is IntegrationStatus.FAILED


# --------------------------------------------------------------------------- #
# MCL helpers and HTTP sink
# --------------------------------------------------------------------------- #


def test_observed_at_prefers_created_time_in_utc() -> None:
    observed = _observed_at({"created": {"time": 1785000000000}})
    assert observed == datetime.fromtimestamp(1785000000, tz=UTC)
    assert observed.tzinfo is UTC


def test_observed_at_falls_back_to_system_metadata() -> None:
    observed = _observed_at({"systemMetadata": {"lastObserved": 1785000000000}})
    assert observed == datetime.fromtimestamp(1785000000, tz=UTC)


def test_same_schema_is_order_independent() -> None:
    before = [SchemaField(name="a"), SchemaField(name="b")]
    after = [SchemaField(name="b"), SchemaField(name="a")]
    assert _same_schema(before, after) is True
    assert _same_schema(before, [SchemaField(name="a")]) is False


def test_event_payload_normalizes_serialized_pegasus_events() -> None:
    class _AsJson:
        def as_json(self) -> bytes:
            return b'{"entityType": "dataset"}'

    assert _event_payload(_AsJson()) == {"entityType": "dataset"}
    assert _event_payload({"already": "mapping"}) == {"already": "mapping"}
    with pytest.raises(ValueError):
        _event_payload(object())


_MCL_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,x.raw.y,PROD)"


def _drift_mcl(urn: str = _MCL_URN) -> dict[str, object]:
    def fields(names: list[str]) -> str:
        return json.dumps({"fields": [{"fieldPath": name} for name in names]})

    return {
        "entityType": "dataset",
        "aspectName": "schemaMetadata",
        "entityUrn": urn,
        "previousAspectValue": {
            "contentType": "application/json",
            "value": fields(["payment_id", "amount"]),
        },
        "aspect": {
            "contentType": "application/json",
            "value": fields(["payment_id", "gross_amount", "net_amount"]),
        },
    }


def test_watcher_rejects_non_object_payloads() -> None:
    watcher = DataHubSchemaMCLWatcher(lambda event: None)
    for payload in (123, "drift", [1, 2, 3]):
        result = watcher.handle(payload)  # type: ignore[arg-type]
        assert result.status is MCLActionStatus.FAILED


def test_watcher_skips_when_sink_rejects_an_out_of_scope_asset() -> None:
    def rejecting_sink(event: SchemaChangeEvent) -> object:
        raise PermissionError("outside allowlist")

    watcher = DataHubSchemaMCLWatcher(rejecting_sink)
    result = watcher.handle(_drift_mcl())
    assert result.status is MCLActionStatus.SKIPPED
    assert "allowlist" in result.reason.lower()


def test_observed_at_falls_back_on_out_of_range_timestamp() -> None:
    observed = _observed_at({"created": {"time": 10**20}})
    assert observed.tzinfo is UTC  # a sane fallback, not a crash


def test_http_event_sink_posts_and_reads_case_reference() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/events/schema-change"
        return httpx.Response(202, json={"case": {"id": "DR-1"}, "deduplicated": False})

    sink = HTTPEventSink("http://api.test", transport=httpx.MockTransport(handler))
    response = sink(
        SchemaChangeEvent(
            entity_urn="urn:li:dataset:test",
            before_fields=[SchemaField(name="amount")],
            after_fields=[SchemaField(name="net_amount")],
        )
    )
    assert response.case.id == "DR-1"
    assert response.deduplicated is False


def test_watcher_skips_identical_schema_rewrite() -> None:
    captured: list[SchemaChangeEvent] = []
    watcher = DataHubSchemaMCLWatcher(captured.append)
    same = json.dumps({"fields": [{"fieldPath": "payment_id"}, {"fieldPath": "amount"}]})
    payload = {
        "entityType": "dataset",
        "aspectName": "schemaMetadata",
        "entityUrn": "urn:li:dataset:x",
        "previousAspectValue": {"contentType": "application/json", "value": same},
        "aspect": {"contentType": "application/json", "value": same},
    }
    result = watcher.handle(payload)
    assert result.status is MCLActionStatus.SKIPPED
    assert captured == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("aspectName", "ownership"),
        ("entityType", "dashboard"),
        ("event_type", "EntityChangeEvent_v1"),
    ],
)
def test_watcher_skips_non_schema_signals(key: str, value: str) -> None:
    payload = _drift_mcl()
    payload[key] = value
    result = DataHubSchemaMCLWatcher(lambda event: None).handle(payload)
    assert result.status is MCLActionStatus.SKIPPED


def test_watcher_fails_on_unsupported_content_type_and_missing_fields() -> None:
    avro = _drift_mcl()
    avro["aspect"] = {"contentType": "application/avro", "value": "..."}
    assert (
        DataHubSchemaMCLWatcher(lambda event: None).handle(avro).status
        is MCLActionStatus.FAILED
    )
    no_fields = _drift_mcl()
    no_fields["aspect"] = {"contentType": "application/json", "value": json.dumps({"nope": []})}
    assert (
        DataHubSchemaMCLWatcher(lambda event: None).handle(no_fields).status
        is MCLActionStatus.FAILED
    )


# --------------------------------------------------------------------------- #
# Guard circuit breaker CLI
# --------------------------------------------------------------------------- #


def test_guard_runs_command_and_propagates_exit_code(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    code = cli_main(
        [
            "guard",
            "--asset",
            DEFAULT_ASSET_URN,
            "--database-path",
            str(settings.resolved_database_path),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(3)",
        ]
    )
    assert code == 3


def test_guard_requires_a_command_after_the_separator(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    code = cli_main(
        [
            "guard",
            "--asset",
            DEFAULT_ASSET_URN,
            "--database-path",
            str(settings.resolved_database_path),
        ]
    )
    assert code == 2


def test_guard_returns_127_when_the_command_cannot_start(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    code = cli_main(
        [
            "guard",
            "--asset",
            DEFAULT_ASSET_URN,
            "--database-path",
            str(settings.resolved_database_path),
            "--",
            "datarescue-nonexistent-binary-please-fail",
        ]
    )
    assert code == 127


def test_guard_exit_code_constant_is_ex_tempfail() -> None:
    assert CONTAINED_EXIT_CODE == 75


# --------------------------------------------------------------------------- #
# Case projection invariant
# --------------------------------------------------------------------------- #


def test_project_case_requires_a_schema_change_detected_root() -> None:
    from apps.api.models import CaseEvent, EventType, utc_now
    from apps.api.store import project_case

    with pytest.raises(ValueError):
        project_case([])

    orphan = CaseEvent(
        sequence=1,
        event_id="e",
        case_id="DR-1",
        event_type=EventType.INCIDENT_RAISED,
        state=CaseState.DETECTED,
        payload={},
        created_at=utc_now(),
    )
    with pytest.raises(ValueError):
        project_case([orphan])


# --------------------------------------------------------------------------- #
# GitHub draft-PR worktree cleanup (retry safety)
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_worktree_cleanup_deletes_leaked_branch_for_retry_safety(tmp_path: Path) -> None:
    from packages.remediation.github import GitHubDraftPRAdapter

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "commit", "--allow-empty", "-m", "init")

    adapter = GitHubDraftPRAdapter(
        enabled=True,
        repository="girginomer10/datarescue",
        repo_root=repo,
        base_branch="main",
        patch_path="demo/dbt/models/staging/stg_payments.sql",
        runtime_dir=tmp_path / "runtime",
    )
    worktree = tmp_path / "runtime" / "worktrees" / "dr-x"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = "datarescue/dr-x"
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "main")
    assert branch in _git(repo, "branch", "--list", branch)

    adapter._cleanup_worktree(worktree, branch)

    # The worktree and the leaked branch ref are both gone...
    assert not worktree.exists()
    assert _git(repo, "branch", "--list", branch) == ""
    # ...so a retry for the same case can re-create the branch cleanly.
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "main")
    assert branch in _git(repo, "branch", "--list", branch)


# --------------------------------------------------------------------------- #
# Single-writer serialization
# --------------------------------------------------------------------------- #


def test_reset_is_serialized_under_the_worker_lock(tmp_path: Path) -> None:
    import threading

    from apps.api.workflow import WorkflowService

    workflow = WorkflowService(make_test_settings(tmp_path))
    # Hold the single-writer lock as if an ingest were mid-flight.
    assert workflow._worker_lock.acquire()
    done = threading.Event()
    captured: dict[str, int] = {}

    def run_reset() -> None:
        captured["sequence"] = workflow.reset()
        done.set()

    worker = threading.Thread(target=run_reset)
    worker.start()
    try:
        # A reset must not interleave within a held write; it blocks on the lock.
        assert done.wait(0.3) is False
    finally:
        workflow._worker_lock.release()
    assert done.wait(2.0) is True
    worker.join()
    assert isinstance(captured["sequence"], int)


def test_append_treats_event_id_collision_as_a_duplicate(tmp_path: Path) -> None:
    from apps.api.models import EventType
    from apps.api.store import DuplicateEventError, EventStore

    store = EventStore(tmp_path / "state.sqlite3")
    payload = {"schema_change": {}, "incident_urn": "urn:li:dataRescueIncident:DR-A"}
    store.append(
        case_id="DR-A",
        event_type=EventType.SCHEMA_CHANGE_DETECTED,
        state=CaseState.DETECTED,
        payload=payload,
        dedup_key="dedup-a",
        event_id="shared-event-id",
    )

    # A different case body reusing the same event_id trips the (event_id,
    # reset_scope) index; it must surface as a duplicate, not a raw 500.
    with pytest.raises(DuplicateEventError) as excinfo:
        store.append(
            case_id="DR-B",
            event_type=EventType.SCHEMA_CHANGE_DETECTED,
            state=CaseState.DETECTED,
            payload={"schema_change": {}, "incident_urn": "urn:li:dataRescueIncident:DR-B"},
            dedup_key="dedup-b",
            event_id="shared-event-id",
        )
    assert excinfo.value.case_id == "DR-A"
