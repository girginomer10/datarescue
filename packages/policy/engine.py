from __future__ import annotations

from pydantic import BaseModel

from apps.api.models import (
    BuildResult,
    CandidateOutcome,
    PolicyCheck,
    PolicyConfig,
    ReconciliationMetrics,
    SemanticVerdict,
)


class PolicyDecision(BaseModel):
    accepted: bool
    checks: list[PolicyCheck]
    reasons: list[str]


class PolicyEngine:
    """Deterministic safety gate. No model output can bypass these checks."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def evaluate(
        self,
        *,
        semantic_verdict: SemanticVerdict,
        metrics: ReconciliationMetrics,
        build: BuildResult,
        lineage_current: bool,
    ) -> PolicyDecision:
        config = self.config
        checks = [
            PolicyCheck(
                name="semantic_evidence",
                passed=(
                    semantic_verdict is SemanticVerdict.MATCH
                    if config.semantic_evidence_required
                    else semantic_verdict is not SemanticVerdict.CONFLICT
                ),
                observed=semantic_verdict.value,
                requirement=(
                    "MATCH required" if config.semantic_evidence_required else "No conflict"
                ),
            ),
            PolicyCheck(
                name="total_variance",
                passed=abs(metrics.total_variance_pct) <= config.max_total_variance_pct,
                observed=f"{metrics.total_variance_pct:.2f}%",
                requirement=f"absolute value <= {config.max_total_variance_pct:.2f}%",
            ),
            PolicyCheck(
                name="row_count_variance",
                passed=abs(metrics.row_count_variance_pct)
                <= config.max_row_count_variance_pct,
                observed=f"{metrics.row_count_variance_pct:.2f}%",
                requirement=f"absolute value <= {config.max_row_count_variance_pct:.2f}%",
            ),
            PolicyCheck(
                name="primary_key_overlap",
                passed=metrics.primary_key_overlap_pct >= config.min_primary_key_overlap_pct,
                observed=f"{metrics.primary_key_overlap_pct:.2f}%",
                requirement=f">= {config.min_primary_key_overlap_pct:.2f}%",
            ),
            PolicyCheck(
                name="null_rate_delta",
                passed=abs(metrics.null_rate_delta_percentage_points)
                <= config.max_null_rate_delta_percentage_points,
                observed=f"{metrics.null_rate_delta_percentage_points:.2f} pp",
                requirement=(
                    "absolute value <= "
                    f"{config.max_null_rate_delta_percentage_points:.2f} pp"
                ),
            ),
            PolicyCheck(
                name="dbt_build",
                passed=build.passed if config.dbt_build_required else True,
                observed=(
                    f"{build.passed_checks}/{build.total_checks} passed"
                    if build.total_checks
                    else ("passed" if build.passed else "failed")
                ),
                requirement="dbt build must pass" if config.dbt_build_required else "optional",
            ),
            PolicyCheck(
                name="lineage_current",
                passed=lineage_current if config.lineage_must_be_current else True,
                observed="current" if lineage_current else "missing or stale",
                requirement=(
                    "current lineage required"
                    if config.lineage_must_be_current
                    else "optional"
                ),
            ),
        ]
        reasons = [
            f"{check.name}: observed {check.observed}; required {check.requirement}"
            for check in checks
            if not check.passed
        ]
        return PolicyDecision(accepted=not reasons, checks=checks, reasons=reasons)

    @staticmethod
    def outcome(decision: PolicyDecision) -> CandidateOutcome:
        return CandidateOutcome.ELIGIBLE if decision.accepted else CandidateOutcome.REJECTED
