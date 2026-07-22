"""HTTP-surface coverage for routes not exercised by the vertical-slice tests.

These assert the API's status-code contract: the health probe, the DataHub MCL
ingest route (skip / fail / enqueue), post-deploy verification guard rails, and
the policy and not-found responses. They run against the in-process replay
workflow, so no Docker or network is required.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.workflow import DEFAULT_ASSET_URN
from tests.backend_helpers import make_test_settings


def _valid_drift_mcl() -> dict[str, object]:
    return {
        "event_type": "MetadataChangeLogEvent_v1",
        "entityType": "dataset",
        "aspectName": "schemaMetadata",
        "entityUrn": DEFAULT_ASSET_URN,
        "previousAspectValue": {
            "contentType": "application/json",
            "value": json.dumps(
                {"fields": [{"fieldPath": "payment_id"}, {"fieldPath": "amount"}]}
            ),
        },
        "aspect": {
            "contentType": "application/json",
            "value": json.dumps(
                {
                    "fields": [
                        {"fieldPath": "payment_id"},
                        {"fieldPath": "gross_amount"},
                        {"fieldPath": "net_amount"},
                    ]
                }
            ),
        },
        "systemMetadata": {"lastObserved": 1785000000000},
    }


def test_health_reports_ok_and_execution_mode(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "replay"


def test_mcl_route_skips_non_schema_aspects(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/events/datahub-mcl",
            json={"entityType": "dataset", "aspectName": "ownership"},
        )
    assert response.status_code == 202
    assert response.json()["status"] == "SKIPPED"


def test_mcl_route_rejects_malformed_schema_metadata_with_422(tmp_path: Path) -> None:
    payload = _valid_drift_mcl()
    payload["aspect"] = {"contentType": "application/json", "value": "{not valid json"}
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.post("/api/v1/events/datahub-mcl", json=payload)
    assert response.status_code == 422


def test_mcl_route_enqueues_real_drift_and_creates_a_case(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.post("/api/v1/events/datahub-mcl", json=_valid_drift_mcl())
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "ENQUEUED"
        case_id = body["case_id"]
        assert case_id

        case = client.get(f"/api/v1/cases/{case_id}")
        assert case.status_code == 200
        assert case.json()["state"] == "PATCH_READY"


def test_mcl_route_skips_out_of_scope_asset_without_500(tmp_path: Path) -> None:
    payload = _valid_drift_mcl()
    payload["entityUrn"] = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,other.raw.secrets,PROD)"
    )
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.post("/api/v1/events/datahub-mcl", json=payload)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "SKIPPED"
    assert "allowlist" in body["reason"].lower()


def test_verify_deployment_requires_a_case_in_pr_open(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        # GitHub writes are disabled, so the safe-repair case stops at PATCH_READY.
        case = client.post("/api/v1/demo/drift", json={}).json()["case"]
        assert case["state"] == "PATCH_READY"
        response = client.post(
            f"/api/v1/cases/{case['id']}/verify-deployment",
            json={
                "merged_commit_sha": "abcdef1",
                "reconciliation": {
                    "total_variance_pct": 0.0,
                    "row_count_variance_pct": 0.0,
                    "primary_key_overlap_pct": 100.0,
                    "null_rate_delta_percentage_points": 0.0,
                },
                "build": {"passed": True, "passed_checks": 8, "total_checks": 8},
            },
        )
    assert response.status_code == 409
    assert "PR_OPEN" in response.json()["detail"]


def test_verify_deployment_unknown_case_is_404(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/cases/DR-NOPE/verify-deployment",
            json={
                "merged_commit_sha": "abcdef1",
                "reconciliation": {
                    "total_variance_pct": 0.0,
                    "row_count_variance_pct": 0.0,
                    "primary_key_overlap_pct": 100.0,
                    "null_rate_delta_percentage_points": 0.0,
                },
                "build": {"passed": True, "passed_checks": 8, "total_checks": 8},
            },
        )
    assert response.status_code == 404


def test_sse_stream_resumes_from_last_event_id_header(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        case_id = client.post("/api/v1/demo/drift", json={}).json()["case"]["id"]
        full = client.get(f"/api/v1/cases/{case_id}/events")
        assert "event: SCHEMA_CHANGE_DETECTED" in full.text
        sequences = [int(value) for value in re.findall(r"^id: (\d+)$", full.text, re.M)]
        assert sequences

        # A reconnect past the newest event replays nothing.
        resumed = client.get(
            f"/api/v1/cases/{case_id}/events",
            headers={"Last-Event-ID": str(max(sequences))},
        )
        assert "event:" not in resumed.text

        # A mid-stream reconnect replays only events after the given id.
        partial = client.get(
            f"/api/v1/cases/{case_id}/events",
            headers={"Last-Event-ID": str(min(sequences))},
        )
        assert "event: SCHEMA_CHANGE_DETECTED" not in partial.text
        assert "event:" in partial.text


def test_unknown_case_lookup_is_404(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        assert client.get("/api/v1/cases/DR-UNKNOWN").status_code == 404


def test_contained_asset_scoping_clears_after_reset(tmp_path: Path) -> None:
    app = create_app(make_test_settings(tmp_path))
    with TestClient(app) as client:
        case = client.post(
            "/api/v1/demo/drift", json={"scenario": "fail-closed"}
        ).json()["case"]
        assert case["state"] == "CONTAINED"

        store = app.state.workflow.store
        contained = store.contained_asset(DEFAULT_ASSET_URN)
        assert contained is not None
        assert contained.id == case["id"]

        # A reset opens a new dedup scope; the guard must no longer see the
        # contained case, so downstream commands are unblocked again.
        assert client.post("/api/v1/demo/reset").status_code == 200
        assert store.contained_asset(DEFAULT_ASSET_URN) is None


def test_policy_endpoint_exposes_the_seven_gate_thresholds(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.get("/api/v1/policy")
    assert response.status_code == 200
    policy = response.json()
    assert policy["max_total_variance_pct"] == 0.50
    assert policy["min_primary_key_overlap_pct"] == 99.90
    assert policy["semantic_evidence_required"] is True
    assert policy["dbt_build_required"] is True
    assert policy["lineage_must_be_current"] is True
