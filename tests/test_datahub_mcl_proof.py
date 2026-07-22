from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from apps.api.config import CANONICAL_ASSET_URN

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify-datahub-mcl-proof.py"
SPEC = importlib.util.spec_from_file_location("verify_datahub_mcl_proof", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _case() -> dict[str, object]:
    return {
        "id": "DR-LIVE",
        "asset_urn": CANONICAL_ASSET_URN,
        "state": "CONTAINED",
        "incident_status": "ACTIVE",
        "incident_integration": {
            "status": "SUCCEEDED",
            "resource_id": "urn:li:incident:dr-live",
            "details": {
                "readback_verified": True,
                "remote_state": "ACTIVE",
            },
        },
        "schema_change": {
            "source": "DATAHUB_MCL",
            "before_fields": [{"name": "payment_id"}, {"name": "amount"}],
            "after_fields": [
                {"name": "payment_id"},
                {"name": "gross_amount"},
                {"name": "net_amount"},
            ],
        },
        "events": [
            {"event_type": "SCHEMA_CHANGE_DETECTED"},
            {"event_type": "INCIDENT_RAISED"},
            {"event_type": "CONTAINED"},
        ],
    }


def test_validator_requires_the_automatic_live_incident_contract() -> None:
    result = MODULE.validate_cases([_case()])

    assert result == {
        "case_id": "DR-LIVE",
        "event_count": 3,
        "incident_urn": "urn:li:incident:dr-live",
        "state": "CONTAINED",
    }


def test_validator_rejects_manual_or_duplicate_cases() -> None:
    manual = _case()
    manual["schema_change"] = {
        **manual["schema_change"],
        "source": "MANUAL",
    }
    with pytest.raises(MODULE.ProofError, match="found 0"):
        MODULE.validate_cases([manual])
    with pytest.raises(MODULE.ProofError, match="found 2"):
        MODULE.validate_cases([_case(), _case()])
