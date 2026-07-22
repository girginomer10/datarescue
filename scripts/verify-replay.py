#!/usr/bin/env python3
"""Verify the immutable DataRescue replay evidence package."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class VerificationError(RuntimeError):
    """Raised when replay evidence is incomplete, altered, or contradictory."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"Cannot read valid JSON from {path}: {error}") from error
    require(isinstance(value, dict), f"Expected a JSON object in {path}")
    return value


def mapping(value: object, label: str) -> Mapping[str, Any]:
    require(isinstance(value, dict), f"Expected object at {label}")
    return value


def values_close(actual: object, expected: float, label: str) -> None:
    require(isinstance(actual, (int, float)), f"Expected numeric value at {label}")
    require(math.isclose(float(actual), expected, abs_tol=1e-9), f"Unexpected {label}: {actual}")


def all_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from all_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from all_strings(nested)


def verify_manifest(manifest_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = manifest_path.resolve()
    root = manifest_path.parent
    manifest = read_object(manifest_path)

    require(manifest.get("manifest_version") == 1, "Unsupported replay manifest version")
    require(manifest.get("hash_algorithm") == "sha256", "Manifest must use SHA-256")
    provenance = mapping(manifest.get("provenance"), "manifest.provenance")
    require(
        provenance.get("status") == "RECORDED_REPLAY",
        "Manifest provenance must be RECORDED_REPLAY",
    )
    integrations = mapping(provenance.get("integrations"), "manifest.provenance.integrations")
    require(
        integrations and set(integrations.values()) == {"NOT_RUN"},
        "Replay manifest must mark every external integration NOT_RUN",
    )

    entries = manifest.get("files")
    require(isinstance(entries, list) and entries, "Manifest files must be a non-empty list")
    paths = [item.get("path") for item in entries if isinstance(item, dict)]
    require(len(paths) == len(entries), "Each manifest file entry must be an object with a path")
    require(all(isinstance(path, str) for path in paths), "Manifest paths must be strings")
    require(paths == sorted(paths), "Manifest file entries must be sorted by path")
    require(len(set(paths)) == len(paths), "Manifest contains duplicate paths")

    discovered = {
        path.relative_to(root).as_posix() for path in (root / "artifacts").rglob("*.json")
    }
    require(set(paths) == discovered, "Manifest does not exactly cover the replay JSON artifacts")

    artifacts: dict[str, dict[str, Any]] = {}
    for raw_entry in entries:
        entry = mapping(raw_entry, "manifest.files[]")
        raw_path = entry["path"]
        require(isinstance(raw_path, str), "Manifest path must be a string")
        relative = PurePosixPath(raw_path)
        require(not relative.is_absolute(), f"Absolute artifact path is forbidden: {raw_path}")
        require(".." not in relative.parts, f"Path traversal is forbidden: {raw_path}")
        target = (root / Path(*relative.parts)).resolve()
        require(target.is_relative_to(root), f"Artifact escapes replay root: {raw_path}")
        require(target.is_file(), f"Missing replay artifact: {raw_path}")

        payload = target.read_bytes()
        expected_size = entry.get("bytes")
        expected_digest = entry.get("sha256")
        require(isinstance(expected_size, int), f"Missing byte size for {raw_path}")
        require(len(payload) == expected_size, f"Byte-size mismatch for {raw_path}")
        require(isinstance(expected_digest, str), f"Missing SHA-256 for {raw_path}")
        require(
            hashlib.sha256(payload).hexdigest() == expected_digest,
            f"SHA-256 mismatch for {raw_path}",
        )

        artifact = read_object(target)
        require(
            artifact.get("artifact_type") == entry.get("artifact_type"),
            f"Artifact type mismatch for {raw_path}",
        )
        artifact_provenance = mapping(artifact.get("provenance"), f"{raw_path}.provenance")
        require(
            artifact_provenance.get("status") in {"RECORDED_REPLAY", "NOT_RUN"},
            f"Replay artifact has unsupported provenance for {raw_path}",
        )
        artifacts[raw_path] = artifact

    listed = set(artifacts)
    for raw_path, artifact in artifacts.items():
        for value in all_strings(artifact):
            prefix = "artifact://replay/"
            if value.startswith(prefix):
                target_path = value.removeprefix(prefix)
                require(
                    target_path in listed,
                    f"Broken artifact reference in {raw_path}: {value}",
                )
    return manifest, artifacts


def verify_claims(manifest: Mapping[str, Any], artifacts: Mapping[str, Mapping[str, Any]]) -> None:
    context = artifacts["artifacts/context-bundle.json"]
    context_provenance = mapping(context["provenance"], "context.provenance")
    context_integrations = mapping(context_provenance["integrations"], "context.integrations")
    require(
        set(context_integrations.values()) == {"NOT_RUN"},
        "Recorded context must not claim a live integration",
    )
    lineage = mapping(context["lineage"], "context.lineage")
    require(lineage.get("current_within_recording") is True, "Recorded lineage must be current")

    gross = artifacts["artifacts/reconciliation/gross_amount.json"]
    gross_metrics = mapping(gross["metrics"], "gross.metrics")
    gross_policy = mapping(gross["policy"], "gross.policy")
    values_close(gross_metrics.get("total_variance_pct"), 3.4, "gross total variance")
    values_close(gross_metrics.get("primary_key_overlap_pct"), 100.0, "gross PK overlap")
    require(gross_policy.get("outcome") == "REJECTED", "Gross candidate must be rejected")

    net = artifacts["artifacts/reconciliation/net_amount.json"]
    net_metrics = mapping(net["metrics"], "net.metrics")
    net_policy = mapping(net["policy"], "net.policy")
    values_close(net_metrics.get("total_variance_pct"), 0.0, "net total variance")
    values_close(net_metrics.get("primary_key_overlap_pct"), 100.0, "net PK overlap")
    require(net_policy.get("outcome") == "SELECTED", "Net candidate must be selected")

    for candidate in ("gross_amount", "net_amount"):
        dbt = artifacts[f"artifacts/dbt/{candidate}.json"]
        result = mapping(dbt["result"], f"dbt.{candidate}.result")
        require(result.get("status") == "PASSED", f"{candidate} dbt build must pass")
        require(result.get("tests_passed") == 8, f"{candidate} must have 8 passing tests")
        require(result.get("tests_total") == 8, f"{candidate} must have 8 total tests")
        require(result.get("tests_failed") == 0, f"{candidate} must have no failed tests")

    case = artifacts["artifacts/cases/DR-024.json"]
    require(case.get("state") == "PATCH_READY", "Replay case must stop at PATCH_READY")
    incident = mapping(case["incident"], "case.incident")
    require(incident.get("local_status") == "ACTIVE", "Replay incident must remain active")
    pull_request = mapping(case["pull_request"], "case.pull_request")
    require(pull_request.get("status") == "NOT_RUN", "Replay must not claim a GitHub PR")
    deployment = mapping(case["deployment"], "case.deployment")
    require(set(deployment.values()) == {"NOT_RUN"}, "Replay must not claim deployment proof")

    evidence = artifacts["artifacts/evidence/DR-024.json"]
    require(evidence.get("recovery_claimed") is False, "Replay must not claim recovery")
    operations = mapping(evidence["external_operations"], "evidence.external_operations")
    require(
        operations and set(operations.values()) == {"NOT_RUN"},
        "Every external replay operation must be NOT_RUN",
    )

    containment = artifacts["artifacts/containment/DR-025.json"]
    require(containment.get("state") == "CONTAINED", "Fail-closed replay must be contained")
    containment_candidate = mapping(containment["candidate"], "containment.candidate")
    require(
        containment_candidate.get("source_field") == "settlement_amount",
        "Containment must evaluate settlement_amount",
    )
    containment_metrics = mapping(
        containment_candidate["reconciliation"], "containment.candidate.reconciliation"
    )
    values_close(
        containment_metrics.get("total_variance_pct"),
        -1.5,
        "containment total variance",
    )
    values_close(
        containment_metrics.get("primary_key_overlap_pct"),
        100.0,
        "containment PK overlap",
    )
    containment_dbt = mapping(containment_candidate["dbt"], "containment.candidate.dbt")
    require(containment_dbt.get("status") == "PASSED", "Containment dbt build must pass")
    require(containment_dbt.get("tests_passed") == 8, "Containment must have 8 tests")
    require(
        containment_candidate.get("policy_outcome") == "REJECTED",
        "Containment candidate must remain rejected",
    )
    effects = mapping(containment["effects"], "containment.effects")
    require(effects.get("guard_contract_exit_code") == 75, "Guard exit code must be 75")
    require(
        effects.get("downstream_command_must_run") is False,
        "Contained downstream command must remain blocked",
    )
    require(effects.get("github_draft_pr_status") == "NOT_RUN", "Containment cannot open a PR")

    expected = mapping(manifest["expected_claims"], "manifest.expected_claims")
    values_close(expected.get("gross_total_variance_pct"), 3.4, "manifest gross claim")
    values_close(expected.get("net_total_variance_pct"), 0.0, "manifest net claim")
    values_close(
        expected.get("containment_total_variance_pct"),
        -1.5,
        "manifest containment claim",
    )
    require(
        expected.get("containment_dbt_tests") == 8,
        "Manifest containment test claim is incorrect",
    )
    require(expected.get("github_replay_status") == "NOT_RUN", "Manifest PR status is unsafe")
    require(expected.get("guard_exit_code") == 75, "Manifest guard claim is incorrect")


def default_manifest() -> Path:
    return Path(__file__).resolve().parents[1] / "demo" / "replay" / "manifest.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=default_manifest(),
        help="path to the replay manifest (defaults to demo/replay/manifest.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest, artifacts = verify_manifest(args.manifest)
        verify_claims(manifest, artifacts)
    except VerificationError as error:
        print(f"Replay verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Replay verification passed: {len(artifacts)} artifacts, hashes and claims valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
