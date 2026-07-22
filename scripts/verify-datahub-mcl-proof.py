#!/usr/bin/env python3
"""Poll the API and validate the credential-free DataHub MCL/incident slice."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

from apps.api.config import CANONICAL_ASSET_URN


class ProofError(AssertionError):
    pass


def validate_cases(cases: object) -> dict[str, Any]:
    if not isinstance(cases, list):
        raise ProofError("Cases response is not a list")
    matches = [
        case
        for case in cases
        if isinstance(case, dict)
        and case.get("asset_urn") == CANONICAL_ASSET_URN
        and isinstance(case.get("schema_change"), dict)
        and case["schema_change"].get("source") == "DATAHUB_MCL"
    ]
    if len(matches) != 1:
        raise ProofError(f"Expected one automatic DATAHUB_MCL case, found {len(matches)}")
    case = matches[0]
    before = {
        field.get("name")
        for field in case["schema_change"].get("before_fields", [])
        if isinstance(field, dict)
    }
    after = {
        field.get("name")
        for field in case["schema_change"].get("after_fields", [])
        if isinstance(field, dict)
    }
    if "amount" not in before or "amount" in after:
        raise ProofError(f"Unexpected pre/post amount fields: before={before}, after={after}")
    if not {"gross_amount", "net_amount"}.issubset(after):
        raise ProofError(f"Drift fields are missing from the MCL: {after}")

    incident = case.get("incident_integration")
    if not isinstance(incident, dict) or incident.get("status") != "SUCCEEDED":
        raise ProofError(f"Live GraphQL incident did not succeed: {incident!r}")
    incident_urn = incident.get("resource_id")
    if not isinstance(incident_urn, str) or not incident_urn.startswith(
        "urn:li:incident:"
    ):
        raise ProofError(f"Invalid live incident URN: {incident_urn!r}")
    details = incident.get("details")
    if (
        not isinstance(details, dict)
        or details.get("readback_verified") is not True
        or details.get("remote_state") != "ACTIVE"
    ):
        raise ProofError("The GraphQL incident has no remote ACTIVE read-back proof")
    if case.get("incident_status") != "ACTIVE":
        raise ProofError("The staged proof must leave the incident ACTIVE")
    if case.get("state") != "CONTAINED":
        raise ProofError(
            "Without MCP/OpenAI credentials the staged proof must fail closed as CONTAINED"
        )

    events = case.get("events")
    if not isinstance(events, list):
        raise ProofError("Case event ledger is missing")
    event_types = {
        event.get("event_type") for event in events if isinstance(event, dict)
    }
    required = {"SCHEMA_CHANGE_DETECTED", "INCIDENT_RAISED", "CONTAINED"}
    if not required.issubset(event_types):
        raise ProofError(f"Case event ledger is incomplete: {event_types}")
    return {
        "case_id": case.get("id"),
        "event_count": len(events),
        "incident_urn": incident_urn,
        "state": case.get("state"),
    }


def poll(api_url: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no case returned"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{api_url.rstrip('/')}/api/v1/cases", timeout=10)
            response.raise_for_status()
            return validate_cases(response.json())
        except (httpx.HTTPError, ValueError, ProofError) as error:
            last_error = str(error)
        time.sleep(1)
    raise ProofError(f"MCL proof did not converge: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--write-baseline", type=Path)
    parser.add_argument("--expect-unchanged", type=Path)
    args = parser.parse_args()

    proof = poll(args.api_url, args.timeout)
    if args.expect_unchanged:
        baseline = json.loads(args.expect_unchanged.read_text(encoding="utf-8"))
        if proof != baseline:
            raise ProofError(
                "Duplicate MCL replay changed the durable case: "
                f"baseline={baseline!r}, current={proof!r}"
            )
    if args.write_baseline:
        args.write_baseline.write_text(
            json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(proof, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
