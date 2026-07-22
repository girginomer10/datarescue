from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

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
    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution: ...


class ReplayEvidenceExecutor:
    """Recorded evidence for the deterministic hackathon fixture.

    This is visibly labelled replay evidence. It is never represented as a live
    Postgres or dbt invocation.
    """

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        known_candidate = proposal.source_field in {"gross_amount", "net_amount"}
        if known_candidate:
            reconciliation_ref = (
                "artifact://replay/artifacts/reconciliation/"
                f"{proposal.source_field}.json"
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
                "dbt build (recorded replay)"
                if recorded_build
                else "dbt build (not recorded)"
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


class PostgresDbtExecutor:
    """Execute an allowlisted candidate in its own Postgres schema and run dbt."""

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

    def execute(
        self, *, case_id: str, proposal: CandidateProposal, context: ContextBundle
    ) -> CandidateExecution:
        try:
            import psycopg
            from psycopg import sql
        except ImportError as error:  # pragma: no cover - dependency is installed in production
            raise RuntimeError("psycopg is required for live candidate execution") from error

        schema = _candidate_schema(case_id, proposal.source_field)
        candidate_query = render_candidate_sql(proposal, relation=self.source_relation)
        candidate_ref = self._write_evidence(schema, "candidate.sql", candidate_query + "\n")
        try:
            with psycopg.connect(self.postgres_dsn, autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(schema)
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP TABLE IF EXISTS {}.payments_fct").format(
                            sql.Identifier(schema)
                        )
                    )
                    cursor.execute(
                        sql.SQL("CREATE TABLE {}.payments_fct AS ").format(
                            sql.Identifier(schema)
                        )
                        + sql.SQL(candidate_query)
                    )
                connection.commit()

            metrics = self._reconcile(schema)
            build = self._run_dbt(schema=schema, source_field=proposal.source_field)
            evidence_refs = [
                f"postgres://execution/{schema}/payments_fct",
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
        import psycopg
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
        candidate = sql.SQL("{}.payments_fct").format(sql.Identifier(schema))
        with psycopg.connect(self.postgres_dsn) as connection, connection.cursor() as cursor:
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
                    "SELECT COUNT(*) FROM {} current "
                    "INNER JOIN {} baseline USING (payment_id)"
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
        metrics.evidence_refs = [
            artifact or f"postgres://execution/{schema}/reconciliation"
        ]
        return metrics

    def _run_dbt(self, *, schema: str, source_field: str) -> BuildResult:
        env = os.environ.copy()
        env["DBT_SCHEMA"] = schema
        env["DATARESCUE_REVENUE_COLUMN"] = require_identifier(source_field, "source field")
        command = [
            "dbt",
            "build",
            "--project-dir",
            str(self.dbt_project_dir),
            "--profiles-dir",
            str(self.dbt_profiles_dir),
            "--target",
            self.dbt_target,
        ]
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
        results_path = self.dbt_project_dir / "target" / "run_results.json"
        passed_checks = 0
        total_checks = 0
        passed_nodes = 0
        total_nodes = 0
        if results_path.exists():
            try:
                results = json.loads(results_path.read_text(encoding="utf-8")).get("results", [])
                nodes = [item for item in results if isinstance(item, dict)]
                tests = [
                    item
                    for item in nodes
                    if str(item.get("unique_id", "")).startswith("test.")
                ]
                total_nodes = len(nodes)
                passed_nodes = sum(
                    1 for item in nodes if item.get("status") in {"pass", "success"}
                )
                total_checks = len(tests)
                passed_checks = sum(
                    1 for item in tests if item.get("status") in {"pass", "success"}
                )
            except (OSError, json.JSONDecodeError):
                pass
        build_artifact: str | None = None
        if results_path.exists() and self.evidence_dir:
            destination = self.evidence_dir / schema / "run_results.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(results_path, destination)
            build_artifact = str(destination)
        return BuildResult(
            passed=completed.returncode == 0,
            passed_checks=passed_checks,
            total_checks=total_checks,
            command=" ".join(command),
            evidence_refs=(
                [build_artifact]
                if build_artifact
                else ([str(results_path)] if results_path.exists() else [])
            ),
            summary=(
                f"dbt exited {completed.returncode}; {passed_checks}/{total_checks} tests passed; "
                f"{passed_nodes}/{total_nodes} total nodes succeeded"
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
        import psycopg
        from psycopg import sql

        with (
            psycopg.connect(self.postgres_dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def _candidate_schema(case_id: str, source_field: str) -> str:
    raw = f"candidate_{case_id}_{source_field}".casefold()
    normalized = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")[:63]
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
