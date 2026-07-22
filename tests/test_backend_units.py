"""Focused unit coverage for safety-critical building blocks.

These tests exercise the deterministic primitives the higher-level workflow
depends on: the state machine, the request contracts, the SQL allowlist,
reconciliation math, the honest DataHub adapters, the MCL helpers, and the
guard circuit breaker. They deliberately avoid the network and Docker so they
run in the fast unit lane while still proving the fail-closed invariants.
"""

from __future__ import annotations

import json
import re
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
from packages.datahub.mcp import (
    CANONICAL_ALLOWED_PARALLEL_LINEAGE_EDGES,
    CANONICAL_CONTEXT_DOCUMENT_TITLE,
    CANONICAL_CONTEXT_DOCUMENT_URN,
    CANONICAL_DIRECT_LINEAGE_EDGES,
    CANONICAL_REQUIRED_LINEAGE_URNS,
    MCP_PROTOCOL_VERSION,
    DataHubMCPAdapter,
    _normalize_context,
)
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

NON_TERMINAL_STATES = [state for state, targets in ALLOWED_TRANSITIONS.items() if targets]
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
def test_backward_and_skip_transitions_are_rejected(current: CaseState, target: CaseState) -> None:
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
        postgres_dsn="postgresql://tester:secret@127.0.0.1:5432/test_database",
        dbt_project_dir=project,
        dbt_profiles_dir=project,
    )

    captured_target: Path | None = None

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal captured_target
        command = args[0]
        assert isinstance(command, list)
        captured_target = Path(command[command.index("--target-path") + 1])
        assert captured_target != project / "target"
        assert not (captured_target / "run_results.json").exists()
        # A compile/parse failure exits non-zero WITHOUT rewriting run_results.json.
        return subprocess.CompletedProcess([], 1, stdout="", stderr="compile error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = executor._run_dbt(schema="candidate_x", source_field="net_amount")

    assert result.passed is False
    assert result.passed_checks == 0
    assert result.total_checks == 0
    assert result.evidence_refs == []
    assert captured_target is not None and not captured_target.exists()
    assert stale.exists()


def test_run_dbt_copies_only_its_isolated_results_to_durable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from packages.evidence.executor import PostgresDbtExecutor

    project = tmp_path / "dbt"
    project.mkdir()
    evidence = tmp_path / "evidence"
    executor = PostgresDbtExecutor(
        postgres_dsn="postgresql://tester:secret@127.0.0.1:5432/test_database",
        dbt_project_dir=project,
        dbt_profiles_dir=project,
        evidence_dir=evidence,
    )
    captured_target: Path | None = None

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal captured_target
        command = args[0]
        assert isinstance(command, list)
        captured_target = Path(command[command.index("--target-path") + 1])
        captured_target.mkdir(parents=True, exist_ok=True)
        (captured_target / "run_results.json").write_text(
            json.dumps(
                {
                    "args": {"which": "build"},
                    "results": [
                        {"unique_id": "model.datarescue.stg_payments", "status": "success"},
                        {"unique_id": "model.datarescue.fct_revenue", "status": "success"},
                        *[
                            {
                                "unique_id": f"test.datarescue.check_{index}",
                                "status": "pass",
                            }
                            for index in range(8)
                        ],
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = executor._run_dbt(schema="candidate_owned", source_field="net_amount")

    durable = evidence / "candidate_owned" / "run_results.json"
    assert result.passed is True
    assert result.passed_checks == result.total_checks == 8
    assert result.evidence_refs == [str(durable)]
    assert json.loads(durable.read_text(encoding="utf-8"))["args"]["which"] == "build"
    assert captured_target is not None and not captured_target.exists()


@pytest.mark.parametrize("artifact_body", [None, "not-json", '{"results": []}'])
def test_run_dbt_rejects_exit_zero_without_valid_complete_build_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_body: str | None,
) -> None:
    import subprocess

    from packages.evidence.executor import PostgresDbtExecutor

    project = tmp_path / "dbt"
    project.mkdir()
    executor = PostgresDbtExecutor(
        postgres_dsn="postgresql://tester:secret@127.0.0.1:5432/test_database",
        dbt_project_dir=project,
        dbt_profiles_dir=project,
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert isinstance(command, list)
        target = Path(command[command.index("--target-path") + 1])
        if artifact_body is not None:
            (target / "run_results.json").write_text(artifact_body, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = executor._run_dbt(schema="candidate_invalid", source_field="net_amount")

    assert result.passed is False
    assert "invalid build evidence" in result.summary


def test_postgres_executor_uses_one_decoded_dsn_identity_for_dbt_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import psycopg

    from packages.evidence.executor import PostgresDbtExecutor

    project = tmp_path / "dbt"
    project.mkdir()
    password = "s3cr:et value"
    executor = PostgresDbtExecutor(
        postgres_dsn=(
            "postgresql://finance%40service:s3cr%3Aet%20value@db.internal:6543/"
            "finance%2Fwarehouse?sslmode=disable"
        ),
        dbt_project_dir=project,
        dbt_profiles_dir=project,
    )
    inherited = {
        "POSTGRES_HOST": "attacker.invalid",
        "POSTGRES_PORT": "9999",
        "POSTGRES_DB": "wrong_database",
        "POSTGRES_USER": "wrong_user",
        "POSTGRES_PASSWORD": "wrong_password",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    captured_dbt_env: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert isinstance(command, list)
        process_env = kwargs["env"]
        assert isinstance(process_env, dict)
        captured_dbt_env.update(
            {
                key: str(process_env[key])
                for key in (
                    "POSTGRES_HOST",
                    "POSTGRES_PORT",
                    "POSTGRES_DB",
                    "POSTGRES_USER",
                    "POSTGRES_PASSWORD",
                )
            }
        )
        target = Path(command[command.index("--target-path") + 1])
        (target / "run_results.json").write_text(
            json.dumps(
                {
                    "args": {"which": "build"},
                    "results": [
                        {
                            "unique_id": "model.datarescue.stg_payments",
                            "status": "success",
                        },
                        {
                            "unique_id": "model.datarescue.fct_revenue",
                            "status": "success",
                        },
                        *[
                            {
                                "unique_id": f"test.datarescue.check_{index}",
                                "status": "pass",
                            }
                            for index in range(8)
                        ],
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    captured_psycopg: dict[str, object] = {}
    connection_marker = object()

    def fake_connect(**kwargs: object) -> object:
        captured_psycopg.update(kwargs)
        return connection_marker

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(psycopg, "connect", fake_connect)

    result = executor._run_dbt(schema="candidate_parity", source_field="net_amount")
    connection = executor._connect()

    expected_dbt_env = {
        "POSTGRES_HOST": "db.internal",
        "POSTGRES_PORT": "6543",
        "POSTGRES_DB": "finance/warehouse",
        "POSTGRES_USER": "finance@service",
        "POSTGRES_PASSWORD": password,
    }
    assert result.passed is True
    assert connection is connection_marker
    assert captured_dbt_env == expected_dbt_env
    assert {
        "host": captured_psycopg["host"],
        "port": str(captured_psycopg["port"]),
        "dbname": captured_psycopg["dbname"],
        "user": captured_psycopg["user"],
        "password": captured_psycopg["password"],
    } == {
        "host": expected_dbt_env["POSTGRES_HOST"],
        "port": expected_dbt_env["POSTGRES_PORT"],
        "dbname": expected_dbt_env["POSTGRES_DB"],
        "user": expected_dbt_env["POSTGRES_USER"],
        "password": expected_dbt_env["POSTGRES_PASSWORD"],
    }
    assert captured_psycopg["sslmode"] == "disable"
    assert password not in result.command
    assert password not in result.summary


@pytest.mark.parametrize(
    "postgres_dsn",
    [
        "host=db.internal dbname=finance user=service password=supersecret",
        "mysql://service:supersecret@db.internal/finance",
        "postgresql://service@db.internal/finance",
        "postgresql://service:supersecret@db-a,db-b/finance",
        "postgresql://service:supersecret@db.internal/finance?sslmode=require",
        "postgresql://service:supersecret@db.internal/finance?application_name=dbt",
        ("postgresql://service:supersecret@db.internal/finance?sslmode=disable&sslmode=disable"),
    ],
)
def test_run_dbt_fails_closed_for_unsupported_or_ambiguous_postgres_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postgres_dsn: str,
) -> None:
    import subprocess

    from packages.evidence.executor import PostgresDbtExecutor

    project = tmp_path / "dbt"
    project.mkdir()
    executor = PostgresDbtExecutor(
        postgres_dsn=postgres_dsn,
        dbt_project_dir=project,
        dbt_profiles_dir=project,
    )

    def unexpected_run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("dbt must not start for an unsupported PostgreSQL DSN")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    result = executor._run_dbt(schema="candidate_invalid_dsn", source_field="net_amount")

    assert result.passed is False
    assert result.passed_checks == 0
    assert result.total_checks == 0
    assert result.evidence_refs == []
    assert result.command == "dbt build (not run: invalid PostgreSQL DSN)"
    assert "dbt was not started" in result.summary
    assert "supersecret" not in result.command
    assert "supersecret" not in result.summary
    with pytest.raises(ValueError) as connection_error:
        executor._connect()
    assert "supersecret" not in str(connection_error.value)


# --------------------------------------------------------------------------- #
# DataHub MCP adapter (mocked transport, no network)
# --------------------------------------------------------------------------- #


def _mcp_handshake(handler_for_call):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "datahub", "version": "0.6.0"},
                    },
                },
                headers={"mcp-session-id": "s1"},
            )
        if method == "notifications/initialized":
            assert request.headers["mcp-protocol-version"] == MCP_PROTOCOL_VERSION
            return httpx.Response(202)
        assert request.headers["mcp-protocol-version"] == MCP_PROTOCOL_VERSION
        response = handler_for_call(body)
        if "text/event-stream" in response.headers.get("content-type", ""):
            return response
        payload = json.loads(response.content) if response.content else None
        if isinstance(payload, dict) and "jsonrpc" not in payload:
            payload = {"jsonrpc": "2.0", "id": body["id"], **payload}
            return httpx.Response(response.status_code, json=payload)
        return response

    return handler


MCP_ASSET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments,PROD)"
MCP_DOWNSTREAM_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.stg_payments,PROD)"
)


def _official_mcp_payload(tool: str) -> dict[str, object]:
    if tool == "get_entities":
        return {
            "urn": MCP_ASSET_URN,
            "ownership": {
                "owners": [
                    {
                        "owner": {
                            "urn": "urn:li:corpGroup:finance-data",
                            "properties": {"displayName": "Finance Data"},
                        }
                    }
                ]
            },
            "glossaryTerms": {
                "terms": [
                    {
                        "term": {
                            "urn": "urn:li:glossaryTerm:NetRevenue",
                            "properties": {
                                "name": "Net Revenue",
                                "description": "Revenue is the net settled amount.",
                            },
                        }
                    }
                ]
            },
            "relatedDocuments": {
                "documents": [
                    {
                        "urn": "urn:li:document:datarescue-runbook",
                        "info": {"title": "DataRescue runbook"},
                    }
                ]
            },
        }
    if tool == "list_schema_fields":
        return {
            "urn": MCP_ASSET_URN,
            "fields": [
                {"fieldPath": "payment_id", "type": "STRING", "nullable": False},
                {"fieldPath": "net_amount", "type": "NUMBER", "nullable": False},
            ],
            "totalFields": 2,
            "returned": 2,
            "remainingCount": 0,
            "matchingCount": None,
            "offset": 0,
        }
    if tool == "get_lineage":
        return {
            "downstreams": {
                "searchResults": [{"entity": {"urn": MCP_DOWNSTREAM_URN}, "degree": 1}],
                "total": 1,
                "returned": 1,
                "hasMore": False,
            }
        }
    raise AssertionError(f"unexpected MCP tool: {tool}")


def _structured_tool_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"result": {"structuredContent": payload}})


def _lineage_payload(*targets: tuple[str, int]) -> dict[str, object]:
    return {
        "downstreams": {
            "searchResults": [
                {"entity": {"urn": urn}, "degree": degree} for urn, degree in targets
            ],
            "total": len(targets),
            "returned": len(targets),
            "hasMore": False,
        }
    }


def _document_grep_payload(
    document_urn: str,
    *,
    title: str,
    content: str,
) -> dict[str, object]:
    return {
        "documents_with_matches": 1,
        "results": [
            {
                "matches": [{"excerpt": content, "position": 0}],
                "title": title,
                "total_matches": 1,
                "urn": document_urn,
            }
        ],
        "total_matches": 1,
    }


def _asset_with_document(document_urn: str) -> dict[str, object]:
    return {
        "urn": MCP_ASSET_URN,
        "relatedDocuments": {"documents": [{"urn": document_urn}]},
    }


def _canonical_direct_lineages() -> dict[str, dict[str, object]]:
    return {
        source_urn: _lineage_payload((target_urn, 1))
        for source_urn, target_urn in CANONICAL_DIRECT_LINEAGE_EDGES
    }


def _canonical_entity_payload() -> dict[str, object]:
    entity = _official_mcp_payload("get_entities")
    entity["urn"] = DEFAULT_ASSET_URN
    entity["ownership"] = {
        "owners": [
            {
                "owner": {
                    "urn": "urn:li:corpuser:finance-data",
                    "properties": {"displayName": "Finance Data"},
                }
            }
        ]
    }
    entity["relatedDocuments"] = {
        "documents": [{"urn": "urn:li:document:datarescue-net-revenue-contract"}]
    }
    return entity


def _canonical_mcp_handler(
    body: dict[str, object],
    *,
    include_context_document_in_asset_page: bool = True,
) -> httpx.Response:
    params = body["params"]  # type: ignore[index]
    tool = params["name"]  # type: ignore[index]
    arguments = params["arguments"]  # type: ignore[index]
    if tool == "get_entities":
        entity = _canonical_entity_payload()
        if not include_context_document_in_asset_page:
            entity["relatedDocuments"] = {
                "documents": [{"urn": f"urn:li:document:unrelated-{index}"} for index in range(10)]
            }
        return _structured_tool_response(entity)
    if tool == "list_schema_fields":
        schema = _official_mcp_payload(tool)
        schema["urn"] = DEFAULT_ASSET_URN
        return _structured_tool_response(schema)
    assert tool == "get_lineage"
    source_urn = arguments["urn"]
    if source_urn == DEFAULT_ASSET_URN and arguments["max_hops"] == 4:
        payload = _lineage_payload(
            *((urn, 1) for urn in CANONICAL_REQUIRED_LINEAGE_URNS if urn != source_urn)
        )
    else:
        payload = _canonical_direct_lineages()[source_urn]  # type: ignore[index]
    return _structured_tool_response(payload)


def test_mcp_endpoint_requires_https_outside_loopback() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        DataHubMCPAdapter(endpoint="http://mcp.example.test/mcp")

    assert DataHubMCPAdapter(endpoint="http://127.0.0.1:8001/mcp").endpoint
    assert DataHubMCPAdapter(endpoint="http://localhost:8001/mcp").endpoint
    assert DataHubMCPAdapter(endpoint="http://[::1]:8001/mcp").endpoint
    assert DataHubMCPAdapter(endpoint="https://mcp.example.test/mcp").endpoint


def test_workflow_uses_separate_mcp_token_not_datahub_gms_token(tmp_path: Path) -> None:
    from apps.api.workflow import WorkflowService

    service = WorkflowService(
        make_test_settings(
            tmp_path,
            datahub_token="gms-secret",
            datahub_mcp_url="https://mcp.example.test/mcp",
            datahub_mcp_token="mcp-secret",
        )
    )

    assert service.mcp.token == "mcp-secret"
    assert service.mcp.token != service.settings.datahub_token
    assert service.mcp.gms_url == service.settings.datahub_gms_url
    assert service.mcp.gms_token == "gms-secret"

    no_mcp_credentials = WorkflowService(
        make_test_settings(
            tmp_path,
            datahub_token="gms-secret",
            datahub_mcp_url="https://mcp.example.test/mcp",
        )
    )
    assert no_mcp_credentials.mcp.token is None
    assert no_mcp_credentials.mcp.gms_token == "gms-secret"


def test_canonical_mcp_context_uses_direct_gms_document_info_beyond_first_page() -> None:
    def gms_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer gms-secret"
        assert b"documentInfo" in request.url.raw_path
        return httpx.Response(
            200,
            json={
                "value": {
                    "title": CANONICAL_CONTEXT_DOCUMENT_TITLE,
                    "relatedAssets": [{"asset": DEFAULT_ASSET_URN}],
                }
            },
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        token="mcp-secret",
        transport=httpx.MockTransport(
            _mcp_handshake(
                lambda body: _canonical_mcp_handler(
                    body, include_context_document_in_asset_page=False
                )
            )
        ),
        gms_url="https://datahub.test",
        gms_token="gms-secret",
        gms_transport=httpx.MockTransport(gms_handler),
        readback_attempts=1,
    )

    result = adapter.fetch_context(DEFAULT_ASSET_URN)

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert CANONICAL_CONTEXT_DOCUMENT_URN in result.context["context_documents"]
    assert result.context["lineage_current"] is True


def test_canonical_mcp_context_fails_when_direct_document_link_is_wrong() -> None:
    def gms_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": {
                    "title": CANONICAL_CONTEXT_DOCUMENT_TITLE,
                    "relatedAssets": [{"asset": "urn:li:dataset:wrong"}],
                }
            },
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(
            _mcp_handshake(
                lambda body: _canonical_mcp_handler(
                    body, include_context_document_in_asset_page=False
                )
            )
        ),
        gms_url="https://datahub.test",
        gms_transport=httpx.MockTransport(gms_handler),
        readback_attempts=1,
    )

    result = adapter.fetch_context(DEFAULT_ASSET_URN)

    assert result.integration.status is IntegrationStatus.FAILED
    assert "not linked" in result.integration.message


def test_mcp_fetch_context_uses_official_composite_tools_and_normalizes() -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        arguments = params["arguments"]  # type: ignore[index]
        captured.append((tool, arguments))
        payload = _official_mcp_payload(tool)
        if tool == "get_entities":
            # get_entities has a union return annotation in FastMCP v3 and can
            # therefore arrive in the official wrapped-result representation.
            return httpx.Response(
                200,
                json={
                    "result": {
                        "structuredContent": {"result": payload},
                        "_meta": {"fastmcp": {"wrap_result": True}},
                    }
                },
            )
        return _structured_tool_response(payload)

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context(MCP_ASSET_URN)

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert [name for name, _ in captured] == [
        "get_entities",
        "list_schema_fields",
        "get_lineage",
    ]
    assert captured[0][1] == {"urns": MCP_ASSET_URN}
    assert captured[2][1]["upstream"] is False
    assert result.context["schema_fields"][1]["fieldPath"] == "net_amount"
    assert result.context["glossary_definition"] == "Revenue is the net settled amount."
    assert result.context["owner"] == "Finance Data"
    assert result.context["lineage_urns"] == [MCP_ASSET_URN, MCP_DOWNSTREAM_URN]
    assert result.context["context_documents"] == [
        "urn:li:glossaryTerm:NetRevenue",
        "urn:li:document:datarescue-runbook",
    ]
    assert result.context["lineage_current"] is True


def test_canonical_mcp_context_marks_an_incomplete_lineage_stale() -> None:
    asset_urn = DEFAULT_ASSET_URN
    entity = _canonical_entity_payload()
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = asset_urn
    lineage = _official_mcp_payload("get_lineage")

    context = _normalize_context(
        asset_urn=asset_urn,
        entity=entity,
        schema=schema,
        lineage=lineage,
        direct_lineages=_canonical_direct_lineages(),
    )

    assert context["lineage_current"] is False


def test_canonical_mcp_context_requires_all_four_direct_edges() -> None:
    asset_urn = DEFAULT_ASSET_URN
    entity = _canonical_entity_payload()
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = asset_urn
    ordered_targets = [target for _source, target in CANONICAL_DIRECT_LINEAGE_EDGES]
    lineage = _lineage_payload(
        *((target, degree) for degree, target in enumerate(ordered_targets, start=1))
    )

    complete = _normalize_context(
        asset_urn=asset_urn,
        entity=entity,
        schema=schema,
        lineage=lineage,
        direct_lineages=_canonical_direct_lineages(),
    )
    assert complete["lineage_current"] is True

    missing_edge = _canonical_direct_lineages()
    missing_edge.pop(CANONICAL_DIRECT_LINEAGE_EDGES[2][0])
    incomplete = _normalize_context(
        asset_urn=asset_urn,
        entity=entity,
        schema=schema,
        lineage=lineage,
        direct_lineages=missing_edge,
    )
    assert incomplete["lineage_current"] is False


def test_canonical_mcp_context_allows_the_native_parallel_physical_edge() -> None:
    asset_urn = DEFAULT_ASSET_URN
    entity = _canonical_entity_payload()
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = asset_urn
    ordered_targets = [target for _source, target in CANONICAL_DIRECT_LINEAGE_EDGES]
    lineage = _lineage_payload(
        *((target, degree) for degree, target in enumerate(ordered_targets, start=1))
    )
    direct_lineages = _canonical_direct_lineages()
    for source_urn, target_urn in CANONICAL_ALLOWED_PARALLEL_LINEAGE_EDGES:
        required_target = dict(CANONICAL_DIRECT_LINEAGE_EDGES)[source_urn]
        direct_lineages[source_urn] = _lineage_payload(
            (required_target, 1),
            (target_urn, 1),
        )

    context = _normalize_context(
        asset_urn=asset_urn,
        entity=entity,
        schema=schema,
        lineage=lineage,
        direct_lineages=direct_lineages,
    )

    assert context["lineage_current"] is True


def test_canonical_mcp_context_rejects_urn_membership_without_direct_topology() -> None:
    asset_urn = DEFAULT_ASSET_URN
    entity = _canonical_entity_payload()
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = asset_urn
    root_targets = tuple((urn, 1) for urn in CANONICAL_REQUIRED_LINEAGE_URNS if urn != asset_urn)
    false_star = _lineage_payload(*root_targets)
    wrong_direct = {
        source_urn: false_star for source_urn, _target_urn in CANONICAL_DIRECT_LINEAGE_EDGES
    }

    context = _normalize_context(
        asset_urn=asset_urn,
        entity=entity,
        schema=schema,
        lineage=false_star,
        direct_lineages=wrong_direct,
    )

    assert context["lineage_current"] is False


def test_canonical_mcp_context_requires_the_seeded_datahub_document() -> None:
    asset_urn = DEFAULT_ASSET_URN
    entity = _canonical_entity_payload()
    entity["relatedDocuments"] = {"documents": []}
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = asset_urn

    with pytest.raises(ValueError, match="context document"):
        _normalize_context(
            asset_urn=asset_urn,
            entity=entity,
            schema=schema,
            lineage=_official_mcp_payload("get_lineage"),
        )


@pytest.mark.parametrize("invalid_contract", ["owner_urn", "owner_display", "glossary"])
def test_canonical_mcp_context_requires_exact_owner_and_glossary(
    invalid_contract: str,
) -> None:
    entity = _canonical_entity_payload()
    if invalid_contract == "owner_urn":
        entity["ownership"]["owners"][0]["owner"]["urn"] = "urn:li:corpuser:other"  # type: ignore[index]
    elif invalid_contract == "owner_display":
        entity["ownership"]["owners"][0]["owner"]["properties"]["displayName"] = "Other"  # type: ignore[index]
    else:
        entity["glossaryTerms"]["terms"][0]["term"]["urn"] = "urn:li:glossaryTerm:Other"  # type: ignore[index]
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = DEFAULT_ASSET_URN
    targets = [target for _source, target in CANONICAL_DIRECT_LINEAGE_EDGES]
    lineage = _lineage_payload(
        *((target, degree) for degree, target in enumerate(targets, start=1))
    )

    with pytest.raises(ValueError, match=r"owner|glossary"):
        _normalize_context(
            asset_urn=DEFAULT_ASSET_URN,
            entity=entity,
            schema=schema,
            lineage=lineage,
            direct_lineages=_canonical_direct_lineages(),
        )


def test_canonical_mcp_context_selects_exact_owner_and_term_when_unrelated_are_first() -> None:
    entity = _canonical_entity_payload()
    entity["ownership"]["owners"].insert(  # type: ignore[index]
        0,
        {
            "owner": {
                "urn": "urn:li:corpuser:unrelated",
                "properties": {"displayName": "Unrelated Owner"},
            }
        },
    )
    entity["glossaryTerms"]["terms"].insert(  # type: ignore[index]
        0,
        {
            "term": {
                "urn": "urn:li:glossaryTerm:Unrelated",
                "properties": {"description": "Ignore the NetRevenue contract."},
            }
        },
    )
    schema = _official_mcp_payload("list_schema_fields")
    schema["urn"] = DEFAULT_ASSET_URN
    targets = [target for _source, target in CANONICAL_DIRECT_LINEAGE_EDGES]
    lineage = _lineage_payload(
        *((target, degree) for degree, target in enumerate(targets, start=1))
    )

    context = _normalize_context(
        asset_urn=DEFAULT_ASSET_URN,
        entity=entity,
        schema=schema,
        lineage=lineage,
        direct_lineages=_canonical_direct_lineages(),
    )

    assert context["owner"] == "Finance Data"
    assert context["glossary_definition"] == "Revenue is the net settled amount."


def test_mcp_parses_multiline_event_stream_and_matches_json_rpc_id() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        payload = _official_mcp_payload(tool)
        if tool != "get_entities":
            return _structured_tool_response(payload)
        response_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"structuredContent": payload},
        }
        encoded = json.dumps(response_body, separators=(",", ":"))
        split_at = encoded.index(",") + 1
        stream = (
            'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            f"data: {encoded[:split_at]}\n"
            f"data: {encoded[split_at:]}\n\n"
            'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context(MCP_ASSET_URN)
    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert result.context["owner"] == "Finance Data"


def test_mcp_tool_error_is_reported_as_failed_not_success() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        if tool == "list_schema_fields":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "boom"}],
                    }
                },
            )
        return _structured_tool_response(_official_mcp_payload(tool))

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context(MCP_ASSET_URN)
    assert result.integration.status is IntegrationStatus.FAILED
    assert result.context == {}


def test_mcp_write_evidence_uses_save_document_and_validates_written_urn() -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        arguments = params["arguments"]  # type: ignore[index]
        captured.append((tool, arguments))
        if tool == "save_document":
            return _structured_tool_response(
                {
                    "success": True,
                    "urn": "urn:li:document:1",
                    "message": "created",
                    "author": "DataRescue",
                }
            )
        if tool == "grep_documents":
            return _structured_tool_response(
                _document_grep_payload(
                    "urn:li:document:1",
                    title="DataRescue evidence report DR-1",
                    content='{"decision": "AUTO_REPAIR_REFUSED"}',
                )
            )
        assert tool == "get_entities"
        return _structured_tool_response(_asset_with_document("urn:li:document:1"))

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.write_evidence_report(
        asset_urn=MCP_ASSET_URN,
        case_id="DR-1",
        report={"decision": "AUTO_REPAIR_REFUSED"},
        degraded=True,
    )
    assert result.status is IntegrationStatus.SUCCEEDED
    assert result.resource_id == "urn:li:document:1"
    assert captured[0][0] == "save_document"
    assert captured[0][1]["document_type"] == "Analysis"
    assert captured[0][1]["topics"] == ["datarescue", "degraded"]
    assert captured[0][1]["related_assets"] == [MCP_ASSET_URN]
    expected_content = '{"decision": "AUTO_REPAIR_REFUSED"}'
    assert captured[1] == (
        "grep_documents",
        {
            "urns": ["urn:li:document:1"],
            "pattern": f"^{re.escape(expected_content)}$",
            "context_chars": 0,
            "max_matches_per_doc": 1,
            "start_offset": 0,
        },
    )
    assert captured[2] == ("get_entities", {"urns": MCP_ASSET_URN})
    assert result.details["readback_verified"] is True


def test_mcp_write_evidence_uses_exact_direct_gms_readback_and_retries() -> None:
    tools_called: list[str] = []
    gms_reads = 0

    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        tools_called.append(tool)
        assert tool == "save_document"
        return _structured_tool_response({"success": True, "urn": "urn:li:document:direct"})

    def gms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gms_reads
        gms_reads += 1
        content = "stale" if gms_reads == 1 else '{"decision": "SAFE"}'
        return httpx.Response(
            200,
            json={
                "value": {
                    "title": "DataRescue evidence report DR-DIRECT",
                    "contents": {"text": content},
                    "relatedAssets": [{"asset": MCP_ASSET_URN}],
                }
            },
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
        gms_url="https://datahub.test",
        gms_transport=httpx.MockTransport(gms_handler),
        readback_attempts=2,
        readback_delay_seconds=0,
    )

    result = adapter.write_evidence_report(
        asset_urn=MCP_ASSET_URN,
        case_id="DR-DIRECT",
        report={"decision": "SAFE"},
        degraded=False,
    )

    assert result.status is IntegrationStatus.SUCCEEDED
    assert tools_called == ["save_document"]
    assert gms_reads == 2
    assert result.details["readback_verified"] is True


def test_mcp_write_evidence_retries_until_exact_document_is_visible() -> None:
    reads = 0

    def call(body: dict[str, object]) -> httpx.Response:
        nonlocal reads
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        if tool == "save_document":
            return _structured_tool_response({"success": True, "urn": "urn:li:document:retry"})
        if tool == "grep_documents":
            reads += 1
            if reads == 1:
                return _structured_tool_response(
                    {"documents_with_matches": 0, "results": [], "total_matches": 0}
                )
            return _structured_tool_response(
                _document_grep_payload(
                    "urn:li:document:retry",
                    title="DataRescue evidence report DR-RETRY",
                    content='{"decision": "SAFE"}',
                )
            )
        assert tool == "get_entities"
        return _structured_tool_response(_asset_with_document("urn:li:document:retry"))

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
        readback_attempts=2,
        readback_delay_seconds=0,
    )
    result = adapter.write_evidence_report(
        asset_urn=MCP_ASSET_URN,
        case_id="DR-RETRY",
        report={"decision": "SAFE"},
        degraded=False,
    )

    assert result.status is IntegrationStatus.SUCCEEDED
    assert reads == 2


def test_mcp_write_evidence_fails_when_readback_does_not_confirm_asset_link() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        if params["name"] == "save_document":  # type: ignore[index]
            return _structured_tool_response({"success": True, "urn": "urn:li:document:unlinked"})
        if params["name"] == "grep_documents":  # type: ignore[index]
            return _structured_tool_response(
                _document_grep_payload(
                    "urn:li:document:unlinked",
                    title="DataRescue evidence report DR-1",
                    content='{"decision": "SAFE"}',
                )
            )
        return _structured_tool_response(
            {"urn": MCP_ASSET_URN, "relatedDocuments": {"documents": []}}
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
        readback_attempts=1,
    )
    result = adapter.write_evidence_report(
        asset_urn=MCP_ASSET_URN,
        case_id="DR-1",
        report={"decision": "SAFE"},
        degraded=False,
    )

    assert result.status is IntegrationStatus.FAILED
    assert result.resource_id is None
    assert "exact DataHub read-back" in result.message


@pytest.mark.parametrize("wrong_field", ["title", "content"])
def test_mcp_write_evidence_fails_when_readback_content_is_not_exact(
    wrong_field: str,
) -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        if params["name"] == "save_document":  # type: ignore[index]
            return _structured_tool_response({"success": True, "urn": "urn:li:document:mismatch"})
        if params["name"] == "grep_documents":  # type: ignore[index]
            title = "Wrong title" if wrong_field == "title" else "DataRescue evidence report DR-1"
            content = "wrong content" if wrong_field == "content" else '{"decision": "SAFE"}'
            return _structured_tool_response(
                _document_grep_payload(
                    "urn:li:document:mismatch",
                    title=title,
                    content=content,
                )
            )
        return _structured_tool_response(_asset_with_document("urn:li:document:mismatch"))

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
        readback_attempts=1,
    )
    result = adapter.write_evidence_report(
        asset_urn=MCP_ASSET_URN,
        case_id="DR-1",
        report={"decision": "SAFE"},
        degraded=False,
    )

    assert result.status is IntegrationStatus.FAILED
    assert "exact DataHub read-back" in result.message


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "urn": None, "message": "denied"},
        {"success": True, "urn": MCP_ASSET_URN, "message": "wrong entity type"},
    ],
)
def test_mcp_write_evidence_fails_without_confirmed_document(
    payload: dict[str, object],
) -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        return _structured_tool_response(payload)

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.write_evidence_report(
        asset_urn=MCP_ASSET_URN,
        case_id="DR-1",
        report={"decision": "AUTO_REPAIR_REFUSED"},
        degraded=True,
    )
    assert result.status is IntegrationStatus.FAILED
    assert result.resource_id is None


def test_mcp_parses_json_text_but_fails_closed_on_prose() -> None:
    def json_text(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        payload = json.dumps(_official_mcp_payload(tool))
        return httpx.Response(
            200,
            json={"result": {"content": [{"type": "text", "text": payload}]}},
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(json_text)),
    )
    assert adapter.fetch_context(MCP_ASSET_URN).integration.status is IntegrationStatus.SUCCEEDED

    def prose_text(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"content": [{"type": "text", "text": "just prose"}]}},
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(prose_text)),
    )
    assert adapter.fetch_context(MCP_ASSET_URN).integration.status is IntegrationStatus.FAILED


@pytest.mark.parametrize("partial_tool", ["list_schema_fields", "get_lineage"])
def test_mcp_context_fails_closed_on_partial_metadata(partial_tool: str) -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        payload = _official_mcp_payload(tool)
        if tool == partial_tool == "list_schema_fields":
            payload["totalFields"] = 3
            payload["remainingCount"] = 1
        if tool == partial_tool == "get_lineage":
            payload["downstreams"]["hasMore"] = True  # type: ignore[index]
        return _structured_tool_response(payload)

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    assert adapter.fetch_context(MCP_ASSET_URN).integration.status is IntegrationStatus.FAILED


@pytest.mark.parametrize("missing_tool", ["list_schema_fields", "get_lineage"])
def test_mcp_context_fails_closed_on_missing_schema_or_lineage(missing_tool: str) -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        params = body["params"]  # type: ignore[index]
        tool = params["name"]  # type: ignore[index]
        payload = _official_mcp_payload(tool)
        if tool == missing_tool == "list_schema_fields":
            payload.update({"fields": [], "totalFields": 0, "returned": 0, "remainingCount": 0})
        if tool == missing_tool == "get_lineage":
            payload["downstreams"].update(  # type: ignore[union-attr]
                {"searchResults": [], "total": 0, "returned": 0}
            )
        return _structured_tool_response(payload)

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    assert adapter.fetch_context(MCP_ASSET_URN).integration.status is IntegrationStatus.FAILED


def test_mcp_empty_event_stream_is_failed() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200, text="event: ping\n\n", headers={"content-type": "text/event-stream"}
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    assert adapter.fetch_context(MCP_ASSET_URN).integration.status is IntegrationStatus.FAILED


def test_mcp_initialize_error_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"message": "handshake refused"},
                },
            )
        return httpx.Response(202)

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(handler)
    )
    assert adapter.fetch_context(MCP_ASSET_URN).integration.status is IntegrationStatus.FAILED


def test_mcp_rejects_unsupported_negotiated_protocol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "old-server", "version": "1"},
                },
            },
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc", transport=httpx.MockTransport(handler)
    )
    result = adapter.fetch_context(MCP_ASSET_URN)

    assert result.integration.status is IntegrationStatus.FAILED
    assert "unsupported protocol version" in result.integration.message


def test_mcp_rejects_json_rpc_response_for_a_different_request() -> None:
    def call(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 999,
                "result": {"structuredContent": _official_mcp_payload("get_entities")},
            },
        )

    adapter = DataHubMCPAdapter(
        endpoint="https://mcp.test/rpc",
        transport=httpx.MockTransport(_mcp_handshake(call)),
    )
    result = adapter.fetch_context(MCP_ASSET_URN)

    assert result.integration.status is IntegrationStatus.FAILED
    assert "does not match request id" in result.integration.message


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


def test_raise_incident_requires_remote_active_readback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": {
                        "entities": ["urn:li:dataset:test"],
                        "title": "DataRescue schema drift DR-1",
                        "status": {"state": "RESOLVED"},
                    }
                },
            )
        return httpx.Response(200, json={"data": {"raiseIncident": "urn:li:incident:1"}})

    adapter = DataHubGraphQLAdapter(
        gms_url="https://datahub.test",
        transport=httpx.MockTransport(handler),
        readback_attempts=1,
        readback_delay_seconds=0,
    )

    result = adapter.raise_incident(
        asset_urn="urn:li:dataset:test", case_id="DR-1", description="drift"
    )

    assert result.status is IntegrationStatus.FAILED
    assert "read-back" in result.message


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
    assert DataHubSchemaMCLWatcher(lambda event: None).handle(avro).status is MCLActionStatus.FAILED
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


def test_append_rejects_event_id_reuse_with_conflicting_content(tmp_path: Path) -> None:
    from apps.api.models import EventType
    from apps.api.store import EventConflictError, EventStore

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

    # An event id is idempotent only when all normalized event content matches.
    # Reusing it for another case is a conflict, never a silent duplicate.
    with pytest.raises(EventConflictError) as excinfo:
        store.append(
            case_id="DR-B",
            event_type=EventType.SCHEMA_CHANGE_DETECTED,
            state=CaseState.DETECTED,
            payload={"schema_change": {}, "incident_urn": "urn:li:dataRescueIncident:DR-B"},
            dedup_key="dedup-b",
            event_id="shared-event-id",
        )
    assert excinfo.value.case_id == "DR-A"


def test_long_case_ids_keep_candidate_schemas_distinct() -> None:
    case_id = f"DR-{'A' * 64}"

    gross = _candidate_schema(case_id, "gross_amount")
    net = _candidate_schema(case_id, "net_amount")

    assert len(gross) <= 63
    assert len(net) <= 63
    assert gross != net
    assert "gross_amount" in gross
    assert "net_amount" in net
