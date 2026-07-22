from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Protocol

from apps.api.config import CANONICAL_ASSET_URN, Settings
from apps.api.models import (
    BuildResult,
    CandidateAssessment,
    CandidateOutcome,
    CandidateProposal,
    CaseSnapshot,
    CaseState,
    ContextBundle,
    EventType,
    IntegrationResult,
    IntegrationStatus,
    PatchArtifact,
    ReconciliationMetrics,
    SchemaChangeEvent,
    SchemaChangeResponse,
    SemanticVerdict,
    VerifyDeploymentRequest,
)
from apps.api.store import DuplicateEventError, EventStore
from packages.datahub.graphql import DataHubGraphQLAdapter
from packages.datahub.mcp import DataHubMCPAdapter
from packages.evidence.executor import (
    CandidateExecution,
    EvidenceExecutor,
    PostgresDbtExecutor,
    ReplayEvidenceExecutor,
)
from packages.policy.engine import PolicyEngine
from packages.remediation.candidates import CandidateGenerator
from packages.remediation.github import GitHubDraftPRAdapter
from packages.remediation.sql_safety import render_dbt_patch

DEFAULT_ASSET_URN = CANONICAL_ASSET_URN


class AssetNotAllowedError(PermissionError):
    pass


class IncidentAdapter(Protocol):
    def raise_incident(
        self, *, asset_urn: str, case_id: str, description: str
    ) -> IntegrationResult: ...

    def resolve_incident(self, *, incident_urn: str) -> IntegrationResult: ...


class WorkflowService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: EventStore | None = None,
        policy: PolicyEngine | None = None,
        mcp: DataHubMCPAdapter | None = None,
        incidents: IncidentAdapter | None = None,
        candidates: CandidateGenerator | None = None,
        executor: EvidenceExecutor | None = None,
        github: GitHubDraftPRAdapter | None = None,
    ) -> None:
        self.settings = settings
        # v1 deliberately serializes recovery work. FastAPI may accept events
        # concurrently, but only one evidence-producing workflow can touch dbt,
        # PostgreSQL candidate schemas, or git worktrees at a time.
        self._worker_lock = threading.Lock()
        self.store = store or EventStore(settings.resolved_database_path)
        self.policy = policy or PolicyEngine()
        self.mcp = mcp or DataHubMCPAdapter(
            endpoint=settings.datahub_mcp_url,
            token=settings.datahub_token,
            context_tool=settings.datahub_mcp_context_tool,
            write_tool=settings.datahub_mcp_write_tool,
        )
        self.incidents = incidents or DataHubGraphQLAdapter(
            gms_url=settings.datahub_gms_url, token=settings.datahub_token
        )
        self.candidates = candidates or CandidateGenerator(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            replay_mode=settings.replay_mode,
        )
        self.executor = executor or _default_executor(settings)
        self.github = github or GitHubDraftPRAdapter(
            enabled=settings.github_write_enabled,
            repository=settings.github_repository,
            repo_root=settings.github_repo_root,
            base_branch=settings.github_base_branch,
            patch_path=settings.github_patch_path,
            runtime_dir=settings.runtime_dir,
        )

    def ingest(self, event: SchemaChangeEvent) -> SchemaChangeResponse:
        if event.entity_urn not in self.settings.allowed_asset_urns:
            raise AssetNotAllowedError(
                f"Asset is outside the configured remediation allowlist: {event.entity_urn}"
            )
        dedup_key = schema_event_dedup_key(event)
        existing = self.store.find_case_for_dedup(dedup_key)
        if existing:
            return SchemaChangeResponse(case=self.store.get_case(existing), deduplicated=True)
        case_id = case_id_from_dedup(dedup_key)
        local_incident_urn = f"urn:li:dataRescueIncident:{case_id}"
        try:
            self.store.append(
                case_id=case_id,
                event_type=EventType.SCHEMA_CHANGE_DETECTED,
                state=CaseState.DETECTED,
                dedup_key=dedup_key,
                event_id=event.event_id,
                payload={
                    "schema_change": event.model_dump(mode="json"),
                    "incident_urn": local_incident_urn,
                },
            )
        except DuplicateEventError as duplicate:
            return SchemaChangeResponse(
                case=self.store.get_case(duplicate.case_id), deduplicated=True
            )
        try:
            with self._worker_lock:
                self._advance(case_id=case_id, event=event)
        except Exception as error:  # keep a durable failure without leaking configuration
            current = self.store.get_case(case_id)
            if current.state not in {CaseState.RESOLVED, CaseState.CONTAINED, CaseState.FAILED}:
                self.store.append(
                    case_id=case_id,
                    event_type=EventType.FAILED,
                    state=CaseState.FAILED,
                    payload={
                        "error_type": type(error).__name__,
                        "message": "Workflow execution failed; inspect server logs for details",
                    },
                )
        return SchemaChangeResponse(case=self.store.get_case(case_id), deduplicated=False)

    def _advance(self, *, case_id: str, event: SchemaChangeEvent) -> None:
        incident_result = self.incidents.raise_incident(
            asset_urn=event.entity_urn,
            case_id=case_id,
            description=(
                "Schema drift detected. DataRescue is gathering evidence; no repair has shipped."
            ),
        )
        self.store.append(
            case_id=case_id,
            event_type=EventType.INCIDENT_RAISED,
            state=CaseState.DETECTED,
            payload={"integration": incident_result.model_dump(mode="json")},
        )
        if not self._connected_integration_ready(
            case_id, incident_result, "DataHub incident creation"
        ):
            return

        context = self._context(event.entity_urn)
        if not context.glossary_definition or not context.lineage_current:
            self._contain(
                case_id,
                event.entity_urn,
                ["Required semantic context or current lineage is unavailable"],
            )
            return
        self.store.append(
            case_id=case_id,
            event_type=EventType.CONTEXT_GATHERED,
            state=CaseState.CONTEXT_GATHERED,
            payload={"context": context.model_dump(mode="json")},
        )

        generated = self.candidates.propose(event, context)
        if not self._connected_integration_ready(
            case_id, generated.integration, "OpenAI candidate generation"
        ):
            return
        if not generated.candidates:
            self._contain(case_id, event.entity_urn, ["No schema-compatible candidate exists"])
            return
        self.store.append(
            case_id=case_id,
            event_type=EventType.CANDIDATES_READY,
            state=CaseState.CANDIDATES_READY,
            payload={
                "candidates": [item.model_dump(mode="json") for item in generated.candidates],
                "generation_integration": generated.integration.model_dump(mode="json"),
            },
        )
        self.store.append(
            case_id=case_id,
            event_type=EventType.VALIDATION_STARTED,
            state=CaseState.VALIDATING,
            payload={
                "candidate_count": len(generated.candidates),
                "execution_mode": self.settings.execution_mode,
            },
        )

        assessments: list[CandidateAssessment] = []
        rejection_reasons: list[str] = []
        for proposal in generated.candidates:
            try:
                execution = self.executor.execute(
                    case_id=case_id, proposal=proposal, context=context
                )
            except Exception as error:
                execution = _failed_execution(type(error).__name__)
            decision = self.policy.evaluate(
                semantic_verdict=execution.semantic_verdict,
                metrics=execution.reconciliation,
                build=execution.build,
                lineage_current=context.lineage_current,
            )
            assessment = CandidateAssessment(
                id=proposal.id,
                source_field=proposal.source_field,
                target_alias=proposal.target_alias,
                rationale=proposal.rationale,
                semantic_verdict=execution.semantic_verdict,
                evidence_refs=execution.evidence_refs,
                reconciliation=execution.reconciliation,
                build=execution.build,
                policy_checks=decision.checks,
                outcome=self.policy.outcome(decision),
            )
            assessments.append(assessment)
            rejection_reasons.extend(
                f"{proposal.source_field}: {reason}" for reason in decision.reasons
            )
            self.store.append(
                case_id=case_id,
                event_type=EventType.CANDIDATE_ASSESSED,
                state=CaseState.VALIDATING,
                payload={"assessment": assessment.model_dump(mode="json")},
            )

        eligible = [item for item in assessments if item.outcome is CandidateOutcome.ELIGIBLE]
        if not eligible:
            self._contain(case_id, event.entity_urn, rejection_reasons)
            return
        selected = min(
            eligible,
            key=lambda item: (
                abs(item.reconciliation.total_variance_pct),
                abs(item.reconciliation.row_count_variance_pct),
                item.source_field,
            ),
        ).model_copy(update={"outcome": CandidateOutcome.SELECTED})
        proposal = next(item for item in generated.candidates if item.id == selected.id)
        patch_path = self.settings.github_repo_root / self.settings.github_patch_path
        patch_content = render_dbt_patch(
            proposal, existing_sql=patch_path.read_text(encoding="utf-8")
        )
        patch = PatchArtifact(
            path=self.settings.github_patch_path,
            content=patch_content,
            sha256=hashlib.sha256(patch_content.encode()).hexdigest(),
        )
        self.store.append(
            case_id=case_id,
            event_type=EventType.PATCH_READY,
            state=CaseState.PATCH_READY,
            payload={
                "selected_candidate": selected.model_dump(mode="json"),
                "patch": patch.model_dump(mode="json"),
            },
        )
        writeback = self.mcp.write_evidence_report(
            asset_urn=event.entity_urn,
            case_id=case_id,
            report={
                "decision": "SAFE_REPAIR_VALIDATED",
                "selected_candidate": selected.model_dump(mode="json"),
                "patch_sha256": patch.sha256,
            },
            degraded=False,
        )
        self.store.append(
            case_id=case_id,
            event_type=EventType.EVIDENCE_WRITTEN,
            state=CaseState.PATCH_READY,
            payload={"integration": writeback.model_dump(mode="json")},
        )
        if not self._connected_integration_ready(
            case_id, writeback, "DataHub patch evidence write-back"
        ):
            return
        pull_request = self.github.create_draft(case_id=case_id, patch=patch)
        if pull_request.integration.status is IntegrationStatus.SUCCEEDED:
            self.store.append(
                case_id=case_id,
                event_type=EventType.PR_OPENED,
                state=CaseState.PR_OPEN,
                payload={"pull_request": pull_request.model_dump(mode="json")},
            )
        else:
            self.store.append(
                case_id=case_id,
                event_type=EventType.PR_ATTEMPTED,
                state=CaseState.PATCH_READY,
                payload={"pull_request": pull_request.model_dump(mode="json")},
            )

    def verify_deployment(
        self, case_id: str, request: VerifyDeploymentRequest
    ) -> CaseSnapshot:
        case = self.store.get_case(case_id)
        if case.state is not CaseState.PR_OPEN:
            raise ValueError("Post-deploy verification requires a real draft PR in PR_OPEN")
        if case.selected_candidate is None:
            raise ValueError("Case has no selected candidate")
        if self.settings.execution_mode.casefold() == "postgres":
            execution, lineage_current = self._live_post_deploy_evidence(case, request)
            semantic_verdict = execution.semantic_verdict
            reconciliation = execution.reconciliation
            build = execution.build
            evidence_mode = "LIVE_RECOMPUTED"
        else:
            semantic_verdict = request.semantic_verdict
            reconciliation = request.reconciliation
            build = request.build
            lineage_current = request.lineage_current
            evidence_mode = "RECORDED_REPLAY_INPUT"
        self.store.append(
            case_id=case_id,
            event_type=EventType.DEPLOYMENT_RECORDED,
            state=CaseState.DEPLOYED,
            payload={
                "merged_commit_sha": request.merged_commit_sha,
                "evidence_mode": evidence_mode,
            },
        )
        decision = self.policy.evaluate(
            semantic_verdict=semantic_verdict,
            metrics=reconciliation,
            build=build,
            lineage_current=lineage_current,
        )
        if not decision.accepted:
            self._contain(case_id, case.asset_urn, decision.reasons)
            return self.store.get_case(case_id)
        self.store.append(
            case_id=case_id,
            event_type=EventType.POST_DEPLOY_VERIFIED,
            state=CaseState.POST_DEPLOY_VERIFIED,
            payload={
                "merged_commit_sha": request.merged_commit_sha,
                "evidence_mode": evidence_mode,
                "reconciliation": reconciliation.model_dump(mode="json"),
                "build": build.model_dump(mode="json"),
                "checks": [check.model_dump(mode="json") for check in decision.checks],
            },
        )
        final_writeback = self.mcp.write_evidence_report(
            asset_urn=case.asset_urn,
            case_id=case_id,
            report={
                "decision": "RECOVERED",
                "merged_commit_sha": request.merged_commit_sha,
                "post_deploy_checks": [
                    check.model_dump(mode="json") for check in decision.checks
                ],
            },
            degraded=False,
        )
        if (
            case.incident_integration
            and case.incident_integration.status is IntegrationStatus.SUCCEEDED
            and final_writeback.status is not IntegrationStatus.SUCCEEDED
        ):
            self.store.append(
                case_id=case_id,
                event_type=EventType.FAILED,
                state=CaseState.FAILED,
                payload={
                    "message": (
                        "Post-deploy data passed, but final DataHub evidence "
                        "write-back failed; incident remains active"
                    ),
                    "integration": final_writeback.model_dump(mode="json"),
                },
            )
            return self.store.get_case(case_id)
        resolution = self.incidents.resolve_incident(incident_urn=case.incident_urn)
        if (
            case.incident_integration
            and case.incident_integration.status is IntegrationStatus.SUCCEEDED
            and resolution.status is not IntegrationStatus.SUCCEEDED
        ):
            self.store.append(
                case_id=case_id,
                event_type=EventType.FAILED,
                state=CaseState.FAILED,
                payload={
                    "message": "Data recovered, but the remote incident could not be resolved",
                    "integration": resolution.model_dump(mode="json"),
                },
            )
            return self.store.get_case(case_id)
        self.store.append(
            case_id=case_id,
            event_type=EventType.INCIDENT_RESOLVED,
            state=CaseState.RESOLVED,
            payload={"integration": resolution.model_dump(mode="json")},
        )
        return self.store.get_case(case_id)

    def _live_post_deploy_evidence(
        self, case: CaseSnapshot, request: VerifyDeploymentRequest
    ) -> tuple[CandidateExecution, bool]:
        """Verify the merge artifact and recompute evidence; ignore caller metrics."""

        if case.selected_candidate is None:
            raise ValueError("Case has no selected candidate")
        repo = self.settings.github_repo_root.resolve()
        patch_path = Path(self.settings.github_patch_path)
        if patch_path.is_absolute() or ".." in patch_path.parts:
            raise ValueError("Configured patch path is not repository-relative")

        def git(*args: str) -> str:
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                error_name = type(error).__name__
                raise ValueError(
                    f"Merged commit verification failed: {error_name}"
                ) from error
            return result.stdout

        commit = request.merged_commit_sha
        git("cat-file", "-e", f"{commit}^{{commit}}")
        git("merge-base", "--is-ancestor", commit, self.settings.github_base_branch)
        merged_model = git("show", f"{commit}:{patch_path.as_posix()}")
        expected_default = (
            'env_var("DATARESCUE_REVENUE_COLUMN", '
            f'"{case.selected_candidate.source_field}")'
        )
        if expected_default not in merged_model:
            raise ValueError("Merged commit does not contain the validated candidate mapping")

        context = self._context(case.asset_urn)
        if context.source == "UNAVAILABLE":
            raise ValueError("Fresh DataHub context is unavailable for post-deploy verification")
        proposal = CandidateProposal(
            id=case.selected_candidate.id,
            source_field=case.selected_candidate.source_field,
            target_alias=case.selected_candidate.target_alias,
            rationale=case.selected_candidate.rationale,
            semantic_evidence=case.selected_candidate.evidence_refs,
        )
        execution = self.executor.execute(case_id=case.id, proposal=proposal, context=context)
        return execution, context.lineage_current

    def _context(self, asset_urn: str) -> ContextBundle:
        result = self.mcp.fetch_context(asset_urn)
        if result.integration.status is IntegrationStatus.SUCCEEDED:
            raw = result.context
            return ContextBundle(
                asset_urn=asset_urn,
                glossary_definition=_string_value(raw.get("glossary_definition")),
                owner=_owner_value(raw.get("owner")),
                lineage_urns=_string_list(raw.get("lineage_urns")),
                context_documents=_string_list(raw.get("context_documents")),
                lineage_current=bool(raw.get("lineage_current", False)),
                source="DATAHUB_MCP",
                integration=result.integration,
            )
        if self.settings.replay_mode:
            integration = result.integration.model_copy(
                update={
                    "details": {
                        **result.integration.details,
                        "fallback": "RECORDED_REPLAY",
                        "live_status": result.integration.status.value,
                    }
                }
            )
            return ContextBundle(
                asset_urn=asset_urn,
                glossary_definition=(
                    "Recognized revenue is the net settled amount after processing fees."
                ),
                owner="Finance Data (demo fixture)",
                lineage_urns=[
                    asset_urn,
                    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.stg_payments,PROD)",
                    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.fct_revenue,PROD)",
                ],
                context_documents=[
                    "urn:li:glossaryTerm:NetRevenue",
                    "artifact://replay/artifacts/context-bundle.json",
                ],
                lineage_current=True,
                source="RECORDED_REPLAY",
                integration=integration,
            )
        return ContextBundle(
            asset_urn=asset_urn,
            source="UNAVAILABLE",
            integration=result.integration,
            lineage_current=False,
        )

    def _contain(self, case_id: str, asset_urn: str, reasons: list[str]) -> None:
        concise = reasons[:20] or ["No candidate satisfies recovery policy"]
        writeback = self.mcp.write_evidence_report(
            asset_urn=asset_urn,
            case_id=case_id,
            report={"decision": "AUTO_REPAIR_REFUSED", "reasons": concise},
            degraded=True,
        )
        self.store.append(
            case_id=case_id,
            event_type=EventType.CONTAINED,
            state=CaseState.CONTAINED,
            payload={
                "reasons": concise,
                "evidence_writeback": writeback.model_dump(mode="json"),
                "guard_exit_code": 75,
            },
        )

    def _connected_integration_ready(
        self,
        case_id: str,
        integration: IntegrationResult,
        label: str,
    ) -> bool:
        if self.settings.replay_mode or integration.status is IntegrationStatus.SUCCEEDED:
            return True
        self.store.append(
            case_id=case_id,
            event_type=EventType.FAILED,
            state=CaseState.FAILED,
            payload={
                "message": f"{label} is required in connected mode",
                "integration": integration.model_dump(mode="json"),
            },
        )
        return False


def _default_executor(settings: Settings) -> EvidenceExecutor:
    if settings.execution_mode.casefold() == "postgres" and settings.postgres_dsn:
        return PostgresDbtExecutor(
            postgres_dsn=settings.postgres_dsn,
            dbt_project_dir=settings.dbt_project_dir,
            dbt_profiles_dir=settings.dbt_profiles_dir,
            dbt_target=settings.dbt_target,
            evidence_dir=settings.runtime_dir / "evidence",
        )
    return ReplayEvidenceExecutor()


def schema_event_dedup_key(event: SchemaChangeEvent) -> str:
    normalized = {
        "entity_urn": event.entity_urn.casefold(),
        "before_fields": sorted(
            (field.model_dump(mode="json") for field in event.before_fields),
            key=lambda item: (item["name"], item["data_type"], item["nullable"]),
        ),
        "after_fields": sorted(
            (field.model_dump(mode="json") for field in event.after_fields),
            key=lambda item: (item["name"], item["data_type"], item["nullable"]),
        ),
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def case_id_from_dedup(dedup_key: str) -> str:
    return f"DR-{dedup_key[:8].upper()}"


def _failed_execution(error_type: str) -> CandidateExecution:
    return CandidateExecution(
        semantic_verdict=SemanticVerdict.MISSING,
        evidence_refs=[],
        reconciliation=ReconciliationMetrics(
            total_variance_pct=1_000_000_000.0,
            row_count_variance_pct=1_000_000_000.0,
            primary_key_overlap_pct=0.0,
            null_rate_delta_percentage_points=1_000_000_000.0,
        ),
        build=BuildResult(
            passed=False,
            summary=f"Candidate execution failed ({error_type}); no success was claimed",
        ),
    )


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _owner_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "urn", "id"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
