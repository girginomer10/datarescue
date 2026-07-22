from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel

from apps.api.models import (
    BuildResult,
    CandidateProposal,
    ContextBundle,
    ReconciliationMetrics,
    SemanticVerdict,
)
from packages.remediation.sql_safety import render_candidate_sql, require_identifier


class CandidateExecution(BaseModel):
    semantic_verdict: SemanticVerdict
    evidence_refs: list[str]
    reconciliation: ReconciliationMetrics
    build: BuildResult


class EvidenceExecutor(Protocol):
    produces_live_evidence: bool

    def bind_to_checkout(
        self, *, checkout_root: Path, repository_root: Path
    ) -> EvidenceExecutor: ...

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution: ...


class ReplayEvidenceExecutor:
    """Recorded evidence for the deterministic hackathon fixture.

    This is visibly labelled replay evidence. It is never represented as a live
    Postgres or dbt invocation.
    """

    produces_live_evidence = False

    def bind_to_checkout(
        self, *, checkout_root: Path, repository_root: Path
    ) -> ReplayEvidenceExecutor:
        del checkout_root, repository_root
        return self

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        known_candidate = proposal.source_field in {"gross_amount", "net_amount"}
        if known_candidate:
            reconciliation_ref = (
                f"artifact://replay/artifacts/reconciliation/{proposal.source_field}.json"
            )
            build_ref = f"artifact://replay/artifacts/dbt/{proposal.source_field}.json"
        else:
            reconciliation_ref = "artifact://replay/artifacts/containment/DR-025.json"
            build_ref = reconciliation_ref
        semantic = _semantic_verdict(proposal.source_field, context)
        if proposal.source_field == "gross_amount":
            metrics = ReconciliationMetrics(
                total_variance_pct=3.40,
                row_count_variance_pct=0.0,
                primary_key_overlap_pct=100.0,
                null_rate_delta_percentage_points=0.0,
                current_row_count=10,
                baseline_row_count=10,
                evidence_refs=[reconciliation_ref],
            )
        elif proposal.source_field == "net_amount":
            metrics = ReconciliationMetrics(
                total_variance_pct=0.0,
                row_count_variance_pct=0.0,
                primary_key_overlap_pct=100.0,
                null_rate_delta_percentage_points=0.0,
                current_row_count=10,
                baseline_row_count=10,
                evidence_refs=[reconciliation_ref],
            )
        elif proposal.source_field == "settlement_amount":
            metrics = ReconciliationMetrics(
                total_variance_pct=-1.50,
                row_count_variance_pct=0.0,
                primary_key_overlap_pct=100.0,
                null_rate_delta_percentage_points=0.0,
                current_row_count=10,
                baseline_row_count=10,
                evidence_refs=[reconciliation_ref],
            )
        else:
            metrics = ReconciliationMetrics(
                total_variance_pct=1_000_000_000.0,
                row_count_variance_pct=1_000_000_000.0,
                primary_key_overlap_pct=0.0,
                null_rate_delta_percentage_points=1_000_000_000.0,
                evidence_refs=[reconciliation_ref],
            )
        recorded_build = proposal.source_field in {
            "gross_amount",
            "net_amount",
            "settlement_amount",
        }
        build = BuildResult(
            passed=recorded_build,
            passed_checks=8 if recorded_build else 0,
            total_checks=8 if recorded_build else 0,
            command=(
                "dbt build (recorded replay)" if recorded_build else "dbt build (not recorded)"
            ),
            evidence_refs=[build_ref],
            summary=(
                "8/8 checks passed in the recorded isolated candidate run"
                if recorded_build
                else "No recorded execution exists for this unrecognized candidate"
            ),
        )
        return CandidateExecution(
            semantic_verdict=semantic,
            evidence_refs=[
                *proposal.semantic_evidence,
                *metrics.evidence_refs,
                *build.evidence_refs,
            ],
            reconciliation=metrics,
            build=build,
        )


_CONNECT_TIMEOUT_SECONDS = 10
_STATEMENT_TIMEOUT_MS = 60_000
_MIN_EXPECTED_DBT_CHECKS = 8
_EXPECTED_DBT_MODEL_SUFFIXES = (".stg_payments", ".fct_revenue")
_DEFAULT_POSTGRES_PORT = 5432
_SUPPORTED_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})
_POSTGRES_DBT_ENV_KEYS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


@dataclass(frozen=True)
class _PostgresConnection:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str = "disable"

    def dbt_environment(self) -> dict[str, str]:
        return {
            "POSTGRES_HOST": self.host,
            "POSTGRES_PORT": str(self.port),
            "POSTGRES_DB": self.dbname,
            "POSTGRES_USER": self.user,
            "POSTGRES_PASSWORD": self.password,
        }


def _parse_postgres_dsn(postgres_dsn: str) -> _PostgresConnection:
    """Parse the one connection identity shared by dbt and reconciliation.

    dbt's checked-in profile accepts five explicit environment variables and
    fixes ``sslmode`` to ``disable``. Accepting libpq keyword DSNs, service
    files, multiple hosts, or additional URI options would let psycopg resolve
    a different endpoint or transport than dbt. Those forms therefore fail
    closed instead of being approximated.
    """

    try:
        parsed = urlsplit(postgres_dsn)
    except ValueError as error:
        raise ValueError("PostgreSQL DSN is not a valid URL") from error
    if parsed.scheme.casefold() not in _SUPPORTED_POSTGRES_SCHEMES:
        raise ValueError("PostgreSQL DSN must use a postgres or postgresql URL")
    if parsed.fragment:
        raise ValueError("PostgreSQL DSN fragments are not supported")
    if parsed.netloc.count("@") != 1:
        raise ValueError("PostgreSQL DSN must contain one explicit credential set")
    if not parsed.hostname or "," in parsed.hostname or "%" in parsed.hostname:
        raise ValueError("PostgreSQL DSN must contain one explicit host")
    if not parsed.path.startswith("/") or not parsed.path[1:] or "/" in parsed.path[1:]:
        raise ValueError("PostgreSQL DSN must contain one URL-encoded database name")

    try:
        port = parsed.port if parsed.port is not None else _DEFAULT_POSTGRES_PORT
    except ValueError as error:
        raise ValueError("PostgreSQL DSN contains an invalid port") from error
    if not 1 <= port <= 65_535:
        raise ValueError("PostgreSQL DSN contains an invalid port")

    username = _decode_dsn_component(parsed.username, "username")
    password = _decode_dsn_component(parsed.password, "password")
    database = _decode_dsn_component(parsed.path[1:], "database name")
    host = parsed.hostname
    if not username or not password or not database:
        raise ValueError("PostgreSQL DSN must contain explicit username, password, and database")
    _reject_control_characters(host, "host")

    try:
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("PostgreSQL DSN contains an invalid query string") from error
    query = dict(query_items)
    if len(query) != len(query_items):
        raise ValueError("PostgreSQL DSN contains duplicate connection options")
    if set(query) - {"sslmode"}:
        raise ValueError("PostgreSQL DSN contains unsupported connection options")
    sslmode = query.get("sslmode", "disable")
    if sslmode != "disable":
        raise ValueError("PostgreSQL DSN sslmode must be disable for the configured dbt profile")

    return _PostgresConnection(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
        sslmode=sslmode,
    )


def _decode_dsn_component(value: str | None, label: str) -> str:
    if value is None:
        return ""
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError(f"PostgreSQL DSN contains invalid encoding in {label}")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"PostgreSQL DSN contains invalid encoding in {label}") from error
    _reject_control_characters(decoded, label)
    return decoded


def _reject_control_characters(value: str, label: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"PostgreSQL DSN contains control characters in {label}")


class PostgresDbtExecutor:
    """Execute an allowlisted candidate in its own Postgres schema and run dbt."""

    produces_live_evidence = True

    def __init__(
        self,
        *,
        postgres_dsn: str,
        dbt_project_dir: Path,
        dbt_profiles_dir: Path,
        dbt_target: str = "dev",
        evidence_dir: Path | None = None,
        source_relation: str = "raw.payments_raw",
        baseline_relation: str = "audit.payments_fct_last_good",
    ) -> None:
        self.postgres_dsn = postgres_dsn
        self.dbt_project_dir = dbt_project_dir.resolve()
        self.dbt_profiles_dir = dbt_profiles_dir.resolve()
        self.dbt_target = require_identifier(dbt_target, "dbt target")
        self.evidence_dir = evidence_dir.resolve() if evidence_dir else None
        self.source_relation = source_relation
        self.baseline_relation = baseline_relation

    def bind_to_checkout(
        self, *, checkout_root: Path, repository_root: Path
    ) -> PostgresDbtExecutor:
        """Clone executor configuration onto an immutable verified worktree."""

        repository = repository_root.resolve()
        checkout = checkout_root.resolve()
        try:
            project_relative = self.dbt_project_dir.relative_to(repository)
        except ValueError as error:
            raise ValueError(
                "Live dbt project must be located inside the configured git repository"
            ) from error
        project = (checkout / project_relative).resolve()
        if checkout != project and checkout not in project.parents:
            raise ValueError("Verified dbt project resolves outside the isolated worktree")
        if not project.is_dir():
            raise ValueError("Verified commit does not contain the configured dbt project")

        try:
            profiles_relative = self.dbt_profiles_dir.relative_to(repository)
        except ValueError:
            profiles = self.dbt_profiles_dir
        else:
            profiles = (checkout / profiles_relative).resolve()
            if checkout != profiles and checkout not in profiles.parents:
                raise ValueError("dbt profiles resolve outside the isolated worktree")
            if not profiles.is_dir():
                raise ValueError("Verified commit does not contain the configured dbt profiles")

        return PostgresDbtExecutor(
            postgres_dsn=self.postgres_dsn,
            dbt_project_dir=project,
            dbt_profiles_dir=profiles,
            dbt_target=self.dbt_target,
            evidence_dir=self.evidence_dir,
            source_relation=self.source_relation,
            baseline_relation=self.baseline_relation,
        )

    def _connect(self, *, autocommit: bool = False):  # type: ignore[no-untyped-def]
        """Open a bounded psycopg connection.

        A wedged database must surface a catchable error instead of hanging the
        single worker thread forever, so every connection carries a connect
        timeout and a server-side statement timeout.
        """
        import psycopg

        connection = _parse_postgres_dsn(self.postgres_dsn)
        return psycopg.connect(
            host=connection.host,
            port=connection.port,
            dbname=connection.dbname,
            user=connection.user,
            password=connection.password,
            sslmode=connection.sslmode,
            autocommit=autocommit,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
        )

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        schema = _candidate_schema(case_id, proposal.source_field)
        candidate_query = render_candidate_sql(proposal, relation=self.source_relation)
        candidate_ref = self._write_evidence(schema, "candidate.sql", candidate_query + "\n")
        try:
            build = self._run_dbt(schema=schema, source_field=proposal.source_field)
            metrics = (
                self._reconcile(schema)
                if build.passed
                else self._failed_reconciliation(schema, build.summary)
            )
            evidence_refs = [
                f"postgres://execution/{schema}/fct_revenue",
                *([candidate_ref] if candidate_ref else []),
                *metrics.evidence_refs,
                *build.evidence_refs,
                *proposal.semantic_evidence,
            ]
            return CandidateExecution(
                semantic_verdict=_semantic_verdict(proposal.source_field, context),
                evidence_refs=evidence_refs,
                reconciliation=metrics,
                build=build,
            )
        finally:
            self._drop_candidate_schema(schema)

    def _reconcile(self, schema: str) -> ReconciliationMetrics:
        from psycopg import sql

        baseline_parts = [
            require_identifier(part, "baseline relation")
            for part in self.baseline_relation.split(".")
        ]
        if len(baseline_parts) != 2:
            raise ValueError("Baseline relation must be schema-qualified")
        baseline = sql.SQL("{}.{}").format(
            sql.Identifier(baseline_parts[0]), sql.Identifier(baseline_parts[1])
        )
        # Reconcile the exact relation produced and tested by dbt. A separately
        # hand-built candidate table could diverge from the model while both
        # the structural build gate and policy metrics appeared green.
        candidate = sql.SQL("{}.fct_revenue").format(sql.Identifier(schema))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT COUNT(*), COALESCE(SUM(revenue), 0), "
                    "COALESCE(AVG(CASE WHEN revenue IS NULL THEN 1.0 ELSE 0.0 END), 0) "
                    "FROM {}"
                ).format(candidate)
            )
            current_row = cursor.fetchone()
            if current_row is None:
                raise RuntimeError("Candidate reconciliation returned no aggregate row")
            current_count, current_total, current_null_rate = current_row
            cursor.execute(
                sql.SQL(
                    "SELECT COUNT(*), COALESCE(SUM(revenue), 0), "
                    "COALESCE(AVG(CASE WHEN revenue IS NULL THEN 1.0 ELSE 0.0 END), 0) "
                    "FROM {}"
                ).format(baseline)
            )
            baseline_row = cursor.fetchone()
            if baseline_row is None:
                raise RuntimeError("Baseline reconciliation returned no aggregate row")
            baseline_count, baseline_total, baseline_null_rate = baseline_row
            cursor.execute(
                sql.SQL(
                    "SELECT COUNT(*) FROM {} current INNER JOIN {} baseline USING (payment_id)"
                ).format(candidate, baseline)
            )
            overlap_row = cursor.fetchone()
            if overlap_row is None:
                raise RuntimeError("Primary-key reconciliation returned no aggregate row")
            overlap_count = int(overlap_row[0])
        current_count = int(current_count)
        baseline_count = int(baseline_count)
        total_variance = _percentage_delta(float(current_total), float(baseline_total))
        row_variance = _percentage_delta(float(current_count), float(baseline_count))
        overlap = 100.0 if baseline_count == 0 else (overlap_count / baseline_count) * 100.0
        null_delta = (float(current_null_rate) - float(baseline_null_rate)) * 100.0
        metrics = ReconciliationMetrics(
            total_variance_pct=total_variance,
            row_count_variance_pct=row_variance,
            primary_key_overlap_pct=overlap,
            null_rate_delta_percentage_points=null_delta,
            current_row_count=current_count,
            baseline_row_count=baseline_count,
        )
        artifact = self._write_evidence(
            schema,
            "reconciliation.json",
            metrics.model_dump_json(indent=2) + "\n",
        )
        metrics.evidence_refs = [artifact or f"postgres://execution/{schema}/reconciliation"]
        return metrics

    def _failed_reconciliation(self, schema: str, build_summary: str) -> ReconciliationMetrics:
        metrics = ReconciliationMetrics(
            total_variance_pct=1_000_000_000.0,
            row_count_variance_pct=1_000_000_000.0,
            primary_key_overlap_pct=0.0,
            null_rate_delta_percentage_points=1_000_000_000.0,
        )
        artifact = self._write_evidence(
            schema,
            "reconciliation-not-run.json",
            json.dumps(
                {
                    "status": "NOT_RUN",
                    "reason": "dbt build evidence did not pass",
                    "build_summary": build_summary,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        metrics.evidence_refs = [
            artifact or f"postgres://execution/{schema}/reconciliation-not-run"
        ]
        return metrics

    def _run_dbt(self, *, schema: str, source_field: str) -> BuildResult:
        env = os.environ.copy()
        try:
            connection = _parse_postgres_dsn(self.postgres_dsn)
        except ValueError as error:
            # The reason strings are deliberately structural and never include
            # the DSN, so a password cannot leak into case evidence or logs.
            return BuildResult(
                passed=False,
                command="dbt build (not run: invalid PostgreSQL DSN)",
                summary=f"dbt was not started: {error}",
            )
        # Always overwrite inherited values. dbt and the reconciliation
        # connection now receive the same decoded host, port, database, user,
        # password, and fixed SSL mode from one DSN parser.
        for key in _POSTGRES_DBT_ENV_KEYS:
            env.pop(key, None)
        env.update(connection.dbt_environment())
        env["DBT_SCHEMA"] = schema
        env["DATARESCUE_REVENUE_COLUMN"] = require_identifier(source_field, "source field")
        # The connected launcher also builds ingestion artifacts from this dbt
        # project. A project-global target directory lets those two independent
        # processes unlink or read each other's run_results.json. Every evidence
        # execution therefore owns a fresh target directory for its entire life.
        with tempfile.TemporaryDirectory(prefix=f"datarescue-dbt-{schema}-") as target:
            target_path = Path(target)
            command = [
                "dbt",
                "build",
                "--project-dir",
                str(self.dbt_project_dir),
                "--profiles-dir",
                str(self.dbt_profiles_dir),
                "--target",
                self.dbt_target,
                "--target-path",
                str(target_path),
            ]
            results_path = target_path / "run_results.json"
            try:
                completed = subprocess.run(
                    command,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return BuildResult(
                    passed=False,
                    command=" ".join(command),
                    summary=f"dbt did not complete: {error}",
                )
            passed_checks = 0
            total_checks = 0
            passed_nodes = 0
            total_nodes = 0
            artifact_error: str | None = None
            model_ids: set[str] = set()
            if not results_path.is_file():
                artifact_error = "run_results.json is missing"
            else:
                try:
                    artifact = json.loads(results_path.read_text(encoding="utf-8"))
                    if not isinstance(artifact, dict):
                        raise ValueError("run_results.json root is not an object")
                    args = artifact.get("args")
                    if not isinstance(args, dict) or args.get("which") != "build":
                        raise ValueError("run_results.json is not from dbt build")
                    results = artifact.get("results")
                    if not isinstance(results, list) or not results:
                        raise ValueError("run_results.json has no result nodes")
                    if not all(isinstance(item, dict) for item in results):
                        raise ValueError("run_results.json contains an invalid result node")
                    nodes = results
                    tests = [
                        item for item in nodes if str(item.get("unique_id", "")).startswith("test.")
                    ]
                    model_ids = {
                        unique_id
                        for item in nodes
                        if (unique_id := str(item.get("unique_id", ""))).startswith("model.")
                    }
                    total_nodes = len(nodes)
                    passed_nodes = sum(
                        1 for item in nodes if item.get("status") in {"pass", "success"}
                    )
                    total_checks = len(tests)
                    passed_checks = sum(
                        1 for item in tests if item.get("status") in {"pass", "success"}
                    )
                    missing_models = [
                        suffix
                        for suffix in _EXPECTED_DBT_MODEL_SUFFIXES
                        if not any(model_id.endswith(suffix) for model_id in model_ids)
                    ]
                    if missing_models:
                        raise ValueError(
                            "run_results.json is missing expected models: "
                            + ", ".join(missing_models)
                        )
                    if total_checks < _MIN_EXPECTED_DBT_CHECKS:
                        raise ValueError(
                            "run_results.json contains fewer than "
                            f"{_MIN_EXPECTED_DBT_CHECKS} dbt tests"
                        )
                except (OSError, json.JSONDecodeError, ValueError) as error:
                    artifact_error = str(error)
            build_artifact: str | None = None
            if results_path.exists() and self.evidence_dir:
                destination = self.evidence_dir / schema / "run_results.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(results_path, destination)
                build_artifact = str(destination)
            passed = (
                completed.returncode == 0
                and artifact_error is None
                and total_nodes > 0
                and passed_nodes == total_nodes
                and passed_checks == total_checks
            )
            artifact_summary = (
                "run_results.json verified"
                if artifact_error is None
                else f"invalid build evidence: {artifact_error}"
            )
            return BuildResult(
                passed=passed,
                passed_checks=passed_checks,
                total_checks=total_checks,
                command=" ".join(command),
                # The isolated target is deleted on return. Only advertise the
                # durable evidence copy, never a path that now belongs to no run.
                evidence_refs=[build_artifact] if build_artifact else [],
                summary=(
                    f"dbt exited {completed.returncode}; "
                    f"{passed_checks}/{total_checks} tests passed; "
                    f"{passed_nodes}/{total_nodes} total nodes succeeded; "
                    f"{artifact_summary}"
                ),
            )

    def _write_evidence(self, schema: str, name: str, content: str) -> str | None:
        if not self.evidence_dir:
            return None
        destination = self.evidence_dir / schema / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return str(destination)

    def _drop_candidate_schema(self, schema: str) -> None:
        from psycopg import sql

        with (
            self._connect(autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def _candidate_schema(case_id: str, source_field: str) -> str:
    case_part = re.sub(r"[^a-z0-9_]+", "_", case_id.casefold()).strip("_")
    source_part = re.sub(r"[^a-z0-9_]+", "_", source_field.casefold()).strip("_")
    normalized = f"candidate_{case_part}_{source_part}"
    if len(normalized) > 63:
        # PostgreSQL truncates identifiers at 63 bytes. Blind truncation can
        # erase the source-field suffix and make two candidates share a schema,
        # overwriting evidence. Keep a readable source hint plus a digest of the
        # complete identity so every long candidate remains distinct.
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
        suffix = f"_{source_part[:16]}_{digest}"
        prefix = normalized[: 63 - len(suffix)].rstrip("_")
        normalized = f"{prefix}{suffix}"
    return require_identifier(normalized, "candidate schema")


def _semantic_verdict(source_field: str, context: ContextBundle) -> SemanticVerdict:
    definition = (context.glossary_definition or "").casefold()
    source = source_field.casefold()
    if "net" in source and "net" in definition:
        return SemanticVerdict.MATCH
    if "gross" in source and "net" in definition:
        return SemanticVerdict.CONFLICT
    return SemanticVerdict.MISSING


def _percentage_delta(current: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0 if current == 0 else 1_000_000_000.0
    return ((current - baseline) / abs(baseline)) * 100.0
