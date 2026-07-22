from __future__ import annotations

from apps.api.models import CaseState


class InvalidStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[CaseState, set[CaseState]] = {
    CaseState.DETECTED: {
        CaseState.CONTEXT_GATHERED,
        CaseState.CONTAINED,
        CaseState.FAILED,
    },
    CaseState.CONTEXT_GATHERED: {
        CaseState.CANDIDATES_READY,
        CaseState.CONTAINED,
        CaseState.FAILED,
    },
    CaseState.CANDIDATES_READY: {
        CaseState.VALIDATING,
        CaseState.CONTAINED,
        CaseState.FAILED,
    },
    CaseState.VALIDATING: {
        CaseState.PATCH_READY,
        CaseState.CONTAINED,
        CaseState.FAILED,
    },
    CaseState.PATCH_READY: {CaseState.PR_OPEN, CaseState.CONTAINED, CaseState.FAILED},
    CaseState.PR_OPEN: {CaseState.DEPLOYED, CaseState.CONTAINED, CaseState.FAILED},
    CaseState.DEPLOYED: {
        CaseState.POST_DEPLOY_VERIFIED,
        CaseState.CONTAINED,
        CaseState.FAILED,
    },
    CaseState.POST_DEPLOY_VERIFIED: {
        CaseState.RESOLVED,
        CaseState.CONTAINED,
        CaseState.FAILED,
    },
    CaseState.RESOLVED: set(),
    CaseState.CONTAINED: set(),
    CaseState.FAILED: set(),
}


def validate_transition(current: CaseState | None, target: CaseState) -> None:
    if current is None:
        if target is not CaseState.DETECTED:
            raise InvalidStateTransition(f"A new case must start in DETECTED, not {target}")
        return
    if current is target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(f"Invalid case transition: {current} -> {target}")
