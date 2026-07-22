#!/usr/bin/env python3
"""Exercise the real Postgres/dbt workflow and prove candidate cleanup."""

from __future__ import annotations

import os
import tempfile
from math import isclose
from pathlib import Path

import psycopg

from apps.api.config import Settings
from apps.api.models import CaseState, SchemaChangeEvent, SchemaField
from apps.api.workflow import DEFAULT_ASSET_URN, WorkflowService


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    dsn = os.environ.get(
        "DATARESCUE_POSTGRES_DSN",
        "postgresql://datarescue:datarescue@127.0.0.1:55432/datarescue",
    )
    with tempfile.TemporaryDirectory(prefix="datarescue-live-proof-") as raw_runtime:
        runtime = Path(raw_runtime)
        settings = Settings(
            replay_mode=True,
            execution_mode="postgres",
            runtime_dir=runtime,
            database_path=runtime / "state.sqlite3",
            postgres_dsn=dsn,
            github_repo_root=repo_root,
            github_write_enabled=False,
            datahub_gms_url=None,
            datahub_mcp_url=None,
            openai_api_key=None,
        )
        event = SchemaChangeEvent(
            entity_urn=DEFAULT_ASSET_URN,
            before_fields=[
                SchemaField(name="payment_id", data_type="text", nullable=False),
                SchemaField(name="amount", data_type="numeric", nullable=False),
            ],
            after_fields=[
                SchemaField(name="payment_id", data_type="text", nullable=False),
                SchemaField(name="gross_amount", data_type="numeric", nullable=False),
                SchemaField(name="net_amount", data_type="numeric", nullable=False),
            ],
            source="CI_POSTGRES_DBT_PROOF",
        )
        result = WorkflowService(settings).ingest(event)
        case = result.case

        assert case.state is CaseState.PATCH_READY
        assert case.selected_candidate is not None
        assert case.selected_candidate.source_field == "net_amount"
        assessments = {item.source_field: item for item in case.candidates}
        assert isclose(
            assessments["gross_amount"].reconciliation.total_variance_pct,
            3.4,
            abs_tol=1e-9,
        )
        assert assessments["gross_amount"].build.passed_checks == 8
        assert assessments["net_amount"].reconciliation.total_variance_pct == 0.0
        assert assessments["net_amount"].reconciliation.primary_key_overlap_pct == 100.0
        assert assessments["net_amount"].build.passed_checks == 8
        assert case.pull_request is not None
        assert case.pull_request.integration.status.value == "NOT_RUN"

        evidence_files = sorted((runtime / "evidence").glob("*/*"))
        assert len(evidence_files) == 6
        assert {path.name for path in evidence_files} == {
            "candidate.sql",
            "reconciliation.json",
            "run_results.json",
        }

        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.schemata "
                "WHERE schema_name LIKE 'candidate_dr_%'"
            )
            row = cursor.fetchone()
        assert row is not None and row[0] == 0

    print(
        "Live workflow verification passed: gross +3.40% rejected, "
        "net 0.00% selected, 8/8 tests, candidate schemas cleaned."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
