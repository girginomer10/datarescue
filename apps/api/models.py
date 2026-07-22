from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class CaseState(StrEnum):
    DETECTED = "DETECTED"
    CONTEXT_GATHERED = "CONTEXT_GATHERED"
    CANDIDATES_READY = "CANDIDATES_READY"
    VALIDATING = "VALIDATING"
    PATCH_READY = "PATCH_READY"
    PR_OPEN = "PR_OPEN"
    DEPLOYED = "DEPLOYED"
    POST_DEPLOY_VERIFIED = "POST_DEPLOY_VERIFIED"
    RESOLVED = "RESOLVED"
    CONTAINED = "CONTAINED"
    FAILED = "FAILED"


class EventType(StrEnum):
    SYSTEM_RESET = "SYSTEM_RESET"
    SCHEMA_CHANGE_DETECTED = "SCHEMA_CHANGE_DETECTED"
    INCIDENT_RAISED = "INCIDENT_RAISED"
    CONTEXT_GATHERED = "CONTEXT_GATHERED"
    CANDIDATES_READY = "CANDIDATES_READY"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    CANDIDATE_ASSESSED = "CANDIDATE_ASSESSED"
    PATCH_READY = "PATCH_READY"
    EVIDENCE_WRITTEN = "EVIDENCE_WRITTEN"
    PR_ATTEMPTED = "PR_ATTEMPTED"
    PR_OPENED = "PR_OPENED"
    DEPLOYMENT_RECORDED = "DEPLOYMENT_RECORDED"
    POST_DEPLOY_VERIFIED = "POST_DEPLOY_VERIFIED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    CONTAINED = "CONTAINED"
    FAILED = "FAILED"


class IntegrationStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    RECORDED_REPLAY = "RECORDED_REPLAY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_RUN = "NOT_RUN"
    FAILED = "FAILED"


class IntegrationResult(BaseModel):
    status: IntegrationStatus
    operation: str
    message: str
    resource_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class SchemaField(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data_type: str = Field(default="unknown", max_length=128)
    nullable: bool = True


class SchemaChangeEvent(BaseModel):
    event_id: str | None = None
    entity_urn: str = Field(min_length=1, max_length=2048)
    before_fields: list[SchemaField]
    after_fields: list[SchemaField]
    observed_at: datetime = Field(default_factory=utc_now)
    source: str = Field(default="DATAHUB_MCL", max_length=64)


class CandidateProposal(BaseModel):
    id: str
    source_field: str
    target_alias: str = "revenue"
    rationale: str
    semantic_evidence: list[str] = Field(default_factory=list)


class SemanticVerdict(StrEnum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"


class CandidateOutcome(StrEnum):
    REJECTED = "REJECTED"
    ELIGIBLE = "ELIGIBLE"
    SELECTED = "SELECTED"


class ReconciliationMetrics(BaseModel):
    total_variance_pct: float
    row_count_variance_pct: float
    primary_key_overlap_pct: float
    null_rate_delta_percentage_points: float
    current_row_count: int | None = None
    baseline_row_count: int | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class BuildResult(BaseModel):
    passed: bool
    passed_checks: int = 0
    total_checks: int = 0
    command: str = "dbt build"
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""


class PolicyCheck(BaseModel):
    name: str
    passed: bool
    observed: str
    requirement: str


class CandidateAssessment(BaseModel):
    id: str
    source_field: str
    target_alias: str
    rationale: str
    semantic_verdict: SemanticVerdict
    evidence_refs: list[str]
    reconciliation: ReconciliationMetrics
    build: BuildResult
    policy_checks: list[PolicyCheck] = Field(default_factory=list)
    outcome: CandidateOutcome = CandidateOutcome.REJECTED


class ContextBundle(BaseModel):
    asset_urn: str
    glossary_definition: str | None = None
    owner: str | None = None
    lineage_urns: list[str] = Field(default_factory=list)
    context_documents: list[str] = Field(default_factory=list)
    lineage_current: bool = False
    source: str
    integration: IntegrationResult


class PatchArtifact(BaseModel):
    path: str
    content: str
    sha256: str


class PullRequestArtifact(BaseModel):
    integration: IntegrationResult
    branch: str | None = None
    url: str | None = None
    bundle_path: str | None = None


class CaseEvent(BaseModel):
    sequence: int
    event_id: str
    case_id: str
    event_type: EventType
    state: CaseState | None
    payload: dict[str, Any]
    created_at: datetime


class CaseSnapshot(BaseModel):
    id: str
    asset_urn: str
    state: CaseState
    created_at: datetime
    updated_at: datetime
    schema_change: SchemaChangeEvent
    incident_urn: str
    incident_status: str = "ACTIVE"
    incident_integration: IntegrationResult | None = None
    context: ContextBundle | None = None
    candidate_generation: IntegrationResult | None = None
    candidate_proposals: list[CandidateProposal] = Field(default_factory=list)
    candidates: list[CandidateAssessment] = Field(default_factory=list)
    selected_candidate: CandidateAssessment | None = None
    patch: PatchArtifact | None = None
    evidence_writeback: IntegrationResult | None = None
    pull_request: PullRequestArtifact | None = None
    containment_reasons: list[str] = Field(default_factory=list)
    deployment_commit: str | None = None
    events: list[CaseEvent] = Field(default_factory=list)


class SchemaChangeResponse(BaseModel):
    case: CaseSnapshot
    deduplicated: bool


class PolicyConfig(BaseModel):
    semantic_evidence_required: bool = True
    max_total_variance_pct: float = 0.50
    max_row_count_variance_pct: float = 0.10
    min_primary_key_overlap_pct: float = 99.90
    max_null_rate_delta_percentage_points: float = 0.50
    dbt_build_required: bool = True
    lineage_must_be_current: bool = True


class VerifyDeploymentRequest(BaseModel):
    merged_commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    reconciliation: ReconciliationMetrics
    build: BuildResult
    semantic_verdict: SemanticVerdict = SemanticVerdict.MATCH
    lineage_current: bool = True

    @model_validator(mode="after")
    def require_build_checks(self) -> VerifyDeploymentRequest:
        if self.build.passed and self.build.total_checks < self.build.passed_checks:
            raise ValueError("passed_checks cannot exceed total_checks")
        return self


class DemoResetResponse(BaseModel):
    reset_sequence: int
    message: str


class DemoDriftRequest(BaseModel):
    scenario: Literal["safe-repair", "fail-closed"] = "safe-repair"
