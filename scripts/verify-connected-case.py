#!/usr/bin/env python3
"""Poll the connected API until one fully evidenced case reaches ``PR_OPEN``."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from apps.api.config import CANONICAL_ASSET_URN

IN_PROGRESS_STATES = {
    "DETECTED",
    "CONTEXT_GATHERED",
    "CANDIDATES_READY",
    "VALIDATING",
    "PATCH_READY",
}
FAIL_CLOSED_STATES = {"CONTAINED", "FAILED"}


class ProofError(AssertionError):
    """The connected workflow did not satisfy its bounded proof contract."""


class TerminalProofError(ProofError):
    """The current case can no longer converge to a valid ``PR_OPEN`` proof."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProofError(f"{label} is missing or is not an object")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProofError(f"{label} is missing or is not a list")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProofError(f"{label} is not numeric: {value!r}")
    return float(value)


def _require_close(value: object, expected: float, label: str) -> None:
    observed = _number(value, label)
    if not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-9):
        raise ProofError(f"{label} must be {expected:.2f}, observed {observed:.8f}")


def _succeeded(value: object, label: str) -> Mapping[str, Any]:
    integration = _mapping(value, label)
    if integration.get("status") != "SUCCEEDED":
        raise ProofError(f"{label} did not succeed: {integration!r}")
    return integration


def _validate_dbt_build(candidate: Mapping[str, Any], label: str, *, exact_checks: bool) -> None:
    build = _mapping(candidate.get("build"), f"{label} dbt build")
    if build.get("passed") is not True:
        raise ProofError(f"{label} did not pass dbt build: {build!r}")
    if exact_checks and (build.get("passed_checks"), build.get("total_checks")) != (8, 8):
        raise ProofError(
            f"{label} must prove 8/8 dbt checks, observed "
            f"{build.get('passed_checks')!r}/{build.get('total_checks')!r}"
        )


def _candidate_for(candidates: list[object], source_field: str) -> Mapping[str, Any]:
    matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, Mapping) and candidate.get("source_field") == source_field
    ]
    if len(matches) != 1:
        raise ProofError(f"Expected exactly one {source_field} candidate, observed {len(matches)}")
    return matches[0]


def _validate_github_url(value: object) -> str:
    if not isinstance(value, str):
        raise ProofError(f"Draft PR URL is missing: {value!r}")
    parsed = urlsplit(value)
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or len(path_parts) != 4
        or path_parts[2] != "pull"
        or not path_parts[3].isdigit()
    ):
        raise ProofError(f"Draft PR URL is not a canonical GitHub pull request: {value!r}")
    return value


def _validate_schema_change(case: Mapping[str, Any]) -> None:
    change = _mapping(case.get("schema_change"), "schema change")
    if change.get("source") != "DATAHUB_MCL":
        raise TerminalProofError(
            f"Current case was not created by the DataHub MCL watcher: {change.get('source')!r}"
        )
    before = {
        field.get("name")
        for field in _list(change.get("before_fields"), "schema change before_fields")
        if isinstance(field, Mapping)
    }
    after = {
        field.get("name")
        for field in _list(change.get("after_fields"), "schema change after_fields")
        if isinstance(field, Mapping)
    }
    if "amount" not in before or "amount" in after:
        raise TerminalProofError(
            f"Current case does not prove the amount removal: before={before}, after={after}"
        )
    if not {"gross_amount", "net_amount"}.issubset(after):
        raise TerminalProofError(f"Current case is missing drift fields: after={after}")


def validate_case(case_value: object) -> dict[str, Any]:
    """Validate the complete connected first-slice acceptance contract."""

    case = _mapping(case_value, "case")
    if case.get("asset_urn") != CANONICAL_ASSET_URN:
        raise TerminalProofError(
            f"Current case monitors the wrong asset: {case.get('asset_urn')!r}"
        )
    _validate_schema_change(case)
    if case.get("state") != "PR_OPEN":
        raise ProofError(f"Connected case has not reached PR_OPEN: {case.get('state')!r}")

    incident = _succeeded(case.get("incident_integration"), "DataHub incident integration")
    incident_urn = incident.get("resource_id")
    if not isinstance(incident_urn, str) or not incident_urn.startswith("urn:li:incident:"):
        raise ProofError(f"DataHub incident URN is invalid: {incident_urn!r}")
    incident_details = _mapping(incident.get("details"), "DataHub incident read-back")
    if (
        incident_details.get("readback_verified") is not True
        or incident_details.get("remote_state") != "ACTIVE"
        or case.get("incident_status") != "ACTIVE"
    ):
        raise ProofError(
            "DataHub incident must have exact remote ACTIVE read-back proof and remain open"
        )

    context = _mapping(case.get("context"), "DataHub context")
    _succeeded(context.get("integration"), "DataHub MCP context integration")
    if context.get("source") != "DATAHUB_MCP":
        raise ProofError(f"Context is not live DataHub MCP evidence: {context.get('source')!r}")
    _succeeded(case.get("candidate_generation"), "OpenAI candidate generation")

    candidates = _list(case.get("candidates"), "candidate assessments")
    gross = _candidate_for(candidates, "gross_amount")
    if gross.get("outcome") != "REJECTED":
        raise ProofError(f"gross_amount must be deterministically rejected: {gross!r}")
    _validate_dbt_build(gross, "gross_amount", exact_checks=False)
    gross_reconciliation = _mapping(gross.get("reconciliation"), "gross_amount reconciliation")
    _require_close(
        gross_reconciliation.get("total_variance_pct"),
        3.4,
        "gross_amount total variance percent",
    )

    net = _candidate_for(candidates, "net_amount")
    selected = _mapping(case.get("selected_candidate"), "selected candidate")
    if net.get("outcome") != "SELECTED" or selected.get("outcome") != "SELECTED":
        raise ProofError("net_amount must be the deterministically selected candidate")
    if selected.get("source_field") != "net_amount" or selected.get("id") != net.get("id"):
        raise ProofError("Selected candidate does not identify the assessed net_amount candidate")
    _validate_dbt_build(selected, "selected net_amount", exact_checks=True)
    net_reconciliation = _mapping(
        selected.get("reconciliation"), "selected net_amount reconciliation"
    )
    _require_close(
        net_reconciliation.get("total_variance_pct"),
        0.0,
        "net_amount total variance percent",
    )
    _require_close(
        net_reconciliation.get("primary_key_overlap_pct"),
        100.0,
        "net_amount primary-key overlap percent",
    )

    writeback = _succeeded(case.get("evidence_writeback"), "DataHub evidence write-back")
    writeback_details = _mapping(writeback.get("details"), "DataHub evidence read-back")
    if writeback_details.get("readback_verified") is not True:
        raise ProofError("DataHub evidence write-back has no exact read-back proof")

    pull_request = _mapping(case.get("pull_request"), "draft pull request")
    pr_integration = _succeeded(pull_request.get("integration"), "GitHub draft PR integration")
    pr_details = _mapping(pr_integration.get("details"), "GitHub draft PR details")
    if pr_details.get("requires_human_merge") is not True:
        raise ProofError("Draft PR does not explicitly require a human merge")
    pr_url = _validate_github_url(pull_request.get("url"))
    if pr_integration.get("resource_id") != pr_url:
        raise ProofError("GitHub integration resource does not match the draft PR URL")

    return {
        "case_id": case.get("id"),
        "incident_urn": incident_urn,
        "gross_variance_pct": 3.4,
        "selected_candidate": "net_amount",
        "selected_variance_pct": 0.0,
        "primary_key_overlap_pct": 100.0,
        "dbt_checks": "8/8",
        "pr_url": pr_url,
        "state": "PR_OPEN",
    }


def validate_current_cases(cases_value: object) -> dict[str, Any] | None:
    """Return a proof at ``PR_OPEN`` or ``None`` while the sole case is progressing."""

    cases = _list(cases_value, "cases response")
    if not cases:
        return None
    if len(cases) != 1:
        raise TerminalProofError(
            f"Expected exactly one current case after reset, found {len(cases)}"
        )
    case = _mapping(cases[0], "current case")
    if case.get("asset_urn") != CANONICAL_ASSET_URN:
        raise TerminalProofError(
            f"Current case monitors the wrong asset: {case.get('asset_urn')!r}"
        )
    _validate_schema_change(case)
    state = case.get("state")
    if state in FAIL_CLOSED_STATES:
        reasons = case.get("containment_reasons")
        raise TerminalProofError(f"Connected case reached terminal state {state}: {reasons!r}")
    if state == "PR_OPEN":
        return validate_case(case)
    if state not in IN_PROGRESS_STATES:
        raise TerminalProofError(f"Connected case reached unexpected state: {state!r}")
    return None


def poll(
    api_url: str,
    timeout_seconds: float,
    *,
    interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ProofError("Connected proof timeout must be greater than zero")
    if interval_seconds < 0:
        raise ProofError("Connected proof poll interval cannot be negative")
    deadline = time.monotonic() + timeout_seconds
    last_status = "no current case returned"
    owned_client = client is None
    active_client = client or httpx.Client(timeout=10)
    try:
        while True:
            try:
                response = active_client.get(f"{api_url.rstrip('/')}/api/v1/cases")
                response.raise_for_status()
                proof = validate_current_cases(response.json())
                if proof is not None:
                    return proof
                payload = response.json()
                if isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
                    last_status = f"current state is {payload[0].get('state')!r}"
                else:
                    last_status = "no current case returned"
            except TerminalProofError:
                raise
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
                last_status = f"API read failed: {error}"

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProofError(
                    f"Connected case did not reach a fully evidenced PR_OPEN within "
                    f"{timeout_seconds:g}s: {last_status}"
                )
            time.sleep(min(interval_seconds, remaining))
    finally:
        if owned_client:
            active_client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--timeout",
        type=float,
        default=os.environ.get("DATARESCUE_CONNECTED_PROOF_TIMEOUT_SECONDS", "600"),
    )
    args = parser.parse_args()

    proof = poll(args.api_url, args.timeout)
    print(json.dumps(proof, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
