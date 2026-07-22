from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
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
from apps.api.state_machine import InvalidStateTransition
from apps.api.store import CaseNotFoundError, DuplicateEventError, EventStore
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
from packages.remediation.github import (
    GitHubDraftPRAdapter,
    _github_repository_from_remote_url,
)
from packages.remediation.sql_safety import render_dbt_patch

DEFAULT_ASSET_URN = CANONICAL_ASSET_URN
_INGEST_RESUMABLE_STATES = frozenset(
    {
        CaseState.DETECTED,
        CaseState.CONTEXT_GATHERED,
        CaseState.CANDIDATES_READY,
        CaseState.VALIDATING,
        CaseState.PATCH_READY,
    }
)


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
        # Duplicate MCL delivery should stay fast while this process is already
        # advancing the same case, but an empty set after restart is the signal
        # to resume a durable non-terminal checkpoint under ``_worker_lock``.
        self._active_cases_lock = threading.Lock()
        self._active_cases: set[str] = set()
        self._settled_cases: set[str] = set()
        self.store = store or EventStore(settings.resolved_database_path)
        self.policy = policy or PolicyEngine()
        self.mcp = mcp or DataHubMCPAdapter(
            endpoint=settings.datahub_mcp_url,
            token=settings.datahub_mcp_token,
            context_tool=settings.datahub_mcp_context_tool,
            write_tool=settings.datahub_mcp_write_tool,
            gms_url=settings.datahub_gms_url,
            gms_token=settings.datahub_token,
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
        self.executor = executor if executor is not None else _default_executor(settings)
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
        # Duplicate MCL deliveries should not queue behind a long dbt run. This
        # optimistic read is safe across reset: get_case() re-reads the active
        # reset scope and falls through when the old case is no longer active.
        existing = self.store.find_case_for_dedup(dedup_key)
        if existing:
            duplicate = self._deduplicated_response(event, dedup_key, existing)
            if duplicate is not None and (
                duplicate.case.state not in _INGEST_RESUMABLE_STATES
                or self._case_is_owned_by_process(existing)
            ):
                return duplicate
        # v1 serializes the entire case-creation flow. The initial detection
        # event and every _advance append must be atomic with respect to a demo
        # reset; otherwise a reset appended between them would split a single
        # case across dedup scopes and permanently break its projection.
        with self._worker_lock:
            existing = self.store.find_case_for_dedup(dedup_key)
            if existing:
                duplicate = self._deduplicated_response(event, dedup_key, existing)
                if duplicate is not None:
                    if (
                        duplicate.case.state in _INGEST_RESUMABLE_STATES
                        and not self._case_is_owned_by_process(existing)
                    ):
                        self._advance_safely_locked(case_id=existing, event=event)
                    return SchemaChangeResponse(
                        case=self.store.get_case(existing), deduplicated=True
                    )

            # Keep the established short canonical ID in the common case, then
            # deterministically widen the digest if that display ID is already
            # occupied by a different dedup key.
            case_id = self._available_case_id(dedup_key)
            for attempt in range(8):
                detection_payload = _detection_payload(event, case_id)
                if event.event_id is not None:
                    duplicate_case = self.store.find_case_for_event_id(
                        event.event_id,
                        case_id=case_id,
                        event_type=EventType.SCHEMA_CHANGE_DETECTED,
                        state=CaseState.DETECTED,
                        payload=detection_payload,
                        dedup_key=dedup_key,
                    )
                    if duplicate_case is not None:
                        return SchemaChangeResponse(
                            case=self.store.get_case(duplicate_case), deduplicated=True
                        )
                try:
                    self.store.append(
                        case_id=case_id,
                        event_type=EventType.SCHEMA_CHANGE_DETECTED,
                        state=CaseState.DETECTED,
                        dedup_key=dedup_key,
                        event_id=event.event_id,
                        payload=detection_payload,
                    )
                    break
                except DuplicateEventError as duplicate:
                    return SchemaChangeResponse(
                        case=self.store.get_case(duplicate.case_id), deduplicated=True
                    )
                except InvalidStateTransition:
                    if attempt == 7:
                        raise
                    # Another API process claimed the short display ID after our
                    # availability read. Re-read and widen instead of conflating
                    # the two drifts.
                    case_id = self._available_case_id(dedup_key)
            self._advance_safely_locked(case_id=case_id, event=event)
            return SchemaChangeResponse(
                case=self.store.get_case(case_id), deduplicated=False
            )

    def _case_is_owned_by_process(self, case_id: str) -> bool:
        with self._active_cases_lock:
            return case_id in self._active_cases or case_id in self._settled_cases

    def _advance_safely_locked(
        self, *, case_id: str, event: SchemaChangeEvent
    ) -> None:
        with self._active_cases_lock:
            self._active_cases.add(case_id)
        completed = False
        try:
            self._advance(case_id=case_id, event=event)
            completed = True
        except Exception as error:  # durable failure without leaking configuration
            current = self.store.get_case(case_id)
            if current.state not in {
                CaseState.RESOLVED,
                CaseState.CONTAINED,
                CaseState.FAILED,
            }:
                self.store.append(
                    case_id=case_id,
                    event_type=EventType.FAILED,
                    state=CaseState.FAILED,
                    payload={
                        "error_type": type(error).__name__,
                        "message": (
                            "Workflow execution failed; inspect server logs for details"
                        ),
                    },
                )
            completed = True
        finally:
            with self._active_cases_lock:
                self._active_cases.discard(case_id)
                if completed:
                    self._settled_cases.add(case_id)

    def _deduplicated_response(
        self, event: SchemaChangeEvent, dedup_key: str, case_id: str
    ) -> SchemaChangeResponse | None:
        if event.event_id is not None:
            self.store.find_case_for_event_id(
                event.event_id,
                case_id=case_id,
                event_type=EventType.SCHEMA_CHANGE_DETECTED,
                state=CaseState.DETECTED,
                payload=_detection_payload(event, case_id),
                dedup_key=dedup_key,
            )
        try:
            case = self.store.get_case(case_id)
        except CaseNotFoundError:
            return None
        return SchemaChangeResponse(case=case, deduplicated=True)

    def _available_case_id(self, dedup_key: str) -> str:
        # Another API process may have inserted this dedup key after the caller's
        # locked re-check. Returning its ID lets append() classify the race as an
        # idempotent duplicate instead of turning it into an availability error.
        existing = self.store.find_case_for_dedup(dedup_key)
        if existing is not None:
            return existing
        preferred = case_id_from_dedup(dedup_key)
        if not self.store.case_id_exists(preferred):
            return preferred
        digest = dedup_key.upper()
        for width in range(16, len(digest) + 1, 8):
            candidate = f"DR-{digest[:width]}"
            if candidate != preferred and not self.store.case_id_exists(candidate):
                return candidate
        raise RuntimeError("Could not allocate a collision-safe case id")

    def reset(self, reason: str = "Demo reset requested") -> int:
        # Serialize the reset against the case-creation flow so a reset can never
        # interleave within a single ingest and split its event stream.
        with self._worker_lock:
            return self.store.reset(reason)

    def _advance(self, *, case_id: str, event: SchemaChangeEvent) -> None:
        case = self.store.get_case(case_id)
        if case.state not in _INGEST_RESUMABLE_STATES:
            return

        incident_result = case.incident_integration
        if incident_result is None:
            incident_result = self.incidents.raise_incident(
                asset_urn=event.entity_urn,
                case_id=case_id,
                description=(
                    "Schema drift detected. DataRescue is gathering evidence; "
                    "no repair has shipped."
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

        case = self.store.get_case(case_id)
        context = case.context
        if context is None:
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
        elif not context.glossary_definition or not context.lineage_current:
            self._contain(
                case_id,
                event.entity_urn,
                ["Persisted semantic context or lineage is incomplete"],
            )
            return

        case = self.store.get_case(case_id)
        proposals = case.candidate_proposals
        generation_integration = case.candidate_generation
        if generation_integration is None or not proposals:
            generated = self.candidates.propose(event, context)
            if not self._connected_integration_ready(
                case_id, generated.integration, "OpenAI candidate generation"
            ):
                return
            if not generated.candidates:
                self._contain(
                    case_id, event.entity_urn, ["No schema-compatible candidate exists"]
                )
                return
            proposals = generated.candidates
            generation_integration = generated.integration
            self.store.append(
                case_id=case_id,
                event_type=EventType.CANDIDATES_READY,
                state=CaseState.CANDIDATES_READY,
                payload={
                    "candidates": [item.model_dump(mode="json") for item in proposals],
                    "generation_integration": generation_integration.model_dump(
                        mode="json"
                    ),
                },
            )
        elif not self._connected_integration_ready(
            case_id, generation_integration, "OpenAI candidate generation"
        ):
            return

        if len({proposal.id for proposal in proposals}) != len(proposals):
            raise ValueError("Candidate proposal ids must be unique")

        case = self.store.get_case(case_id)
        if case.state is CaseState.CANDIDATES_READY:
            self.store.append(
                case_id=case_id,
                event_type=EventType.VALIDATION_STARTED,
                state=CaseState.VALIDATING,
                payload={
                    "candidate_count": len(proposals),
                    "execution_mode": self.settings.execution_mode,
                },
            )

        case = self.store.get_case(case_id)
        assessments_by_id = {assessment.id: assessment for assessment in case.candidates}
        rejection_reasons: list[str] = []
        for proposal in proposals:
            existing_assessment = assessments_by_id.get(proposal.id)
            if existing_assessment is not None:
                rejection_reasons.extend(
                    f"{proposal.source_field}: {check.name}"
                    for check in existing_assessment.policy_checks
                    if not check.passed
                )
                continue
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
            assessments_by_id[assessment.id] = assessment
            rejection_reasons.extend(
                f"{proposal.source_field}: {reason}" for reason in decision.reasons
            )
            self.store.append(
                case_id=case_id,
                event_type=EventType.CANDIDATE_ASSESSED,
                state=CaseState.VALIDATING,
                payload={"assessment": assessment.model_dump(mode="json")},
            )

        assessments = [assessments_by_id[proposal.id] for proposal in proposals]
        eligible = [
            item
            for item in assessments
            if item.outcome in {CandidateOutcome.ELIGIBLE, CandidateOutcome.SELECTED}
        ]
        if not eligible:
            self._contain(case_id, event.entity_urn, rejection_reasons)
            return

        case = self.store.get_case(case_id)
        selected = case.selected_candidate
        patch = case.patch
        if selected is None or patch is None:
            selected = min(
                eligible,
                key=lambda item: (
                    abs(item.reconciliation.total_variance_pct),
                    abs(item.reconciliation.row_count_variance_pct),
                    item.source_field,
                ),
            ).model_copy(update={"outcome": CandidateOutcome.SELECTED})
            proposal = next(item for item in proposals if item.id == selected.id)
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

        case = self.store.get_case(case_id)
        writeback = case.evidence_writeback
        if writeback is None:
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

        case = self.store.get_case(case_id)
        pull_request = case.pull_request
        if (
            pull_request is not None
            and pull_request.integration.status is IntegrationStatus.SUCCEEDED
        ):
            if case.state is CaseState.PATCH_READY:
                self.store.append(
                    case_id=case_id,
                    event_type=EventType.PR_OPENED,
                    state=CaseState.PR_OPEN,
                    payload={"pull_request": pull_request.model_dump(mode="json")},
                )
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
        # Post-deploy verification recomputes evidence against Postgres/dbt/git in
        # postgres mode, so it must hold the same single-writer lock as ingest and
        # re-check the case state inside it. This blocks concurrent duplicate
        # verifications from double-recording a deployment or double-resolving.
        with self._worker_lock:
            return self._verify_deployment_locked(case_id, request)

    def _verify_deployment_locked(
        self, case_id: str, request: VerifyDeploymentRequest
    ) -> CaseSnapshot:
        case = self.store.get_case(case_id)
        if case.state is not CaseState.PR_OPEN:
            raise ValueError("Post-deploy verification requires a real draft PR in PR_OPEN")
        if case.selected_candidate is None:
            raise ValueError("Case has no selected candidate")
        if self.settings.execution_mode.strip().casefold() == "postgres":
            if not _produces_live_evidence(self.executor):
                raise ValueError(
                    "Post-deploy verification requires an executor that explicitly "
                    "produces live evidence"
                )
            execution, lineage_current, verified_commit = self._live_post_deploy_evidence(
                case, request
            )
            semantic_verdict = execution.semantic_verdict
            reconciliation = execution.reconciliation
            build = execution.build
            evidence_mode = "LIVE_RECOMPUTED"
        else:
            if (
                case.incident_integration is not None
                and case.incident_integration.status is IntegrationStatus.SUCCEEDED
            ):
                # A live DataHub incident must never be resolved on caller-supplied
                # metrics and an unverified commit SHA. Recomputation (git merge
                # verification + fresh evidence) requires execution_mode=postgres;
                # refuse rather than trust request-provided numbers.
                raise ValueError(
                    "Resolving a live incident requires postgres execution mode "
                    "to recompute post-deploy evidence"
                )
            semantic_verdict = request.semantic_verdict
            reconciliation = request.reconciliation
            build = request.build
            lineage_current = request.lineage_current
            evidence_mode = "RECORDED_REPLAY_INPUT"
            verified_commit = request.merged_commit_sha
        self.store.append(
            case_id=case_id,
            event_type=EventType.DEPLOYMENT_RECORDED,
            state=CaseState.DEPLOYED,
            payload={
                "merged_commit_sha": verified_commit,
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
                "merged_commit_sha": verified_commit,
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
                "merged_commit_sha": verified_commit,
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
    ) -> tuple[CandidateExecution, bool, str]:
        """Verify and execute only the exact artifact reachable from origin/base."""

        if case.selected_candidate is None:
            raise ValueError("Case has no selected candidate")
        if case.patch is None:
            raise ValueError("Case has no stored patch artifact to verify")
        repo = self.settings.github_repo_root.resolve()
        configured_patch_path = Path(self.settings.github_patch_path)
        stored_patch_path = Path(case.patch.path)
        if (
            configured_patch_path.is_absolute()
            or ".." in configured_patch_path.parts
            or stored_patch_path.is_absolute()
            or ".." in stored_patch_path.parts
        ):
            raise ValueError("Configured patch path is not repository-relative")
        if stored_patch_path != configured_patch_path:
            raise ValueError("Stored patch path does not match the configured repository path")

        def git(*args: str, timeout: int = 30) -> bytes:
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    timeout=timeout,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                error_name = type(error).__name__
                raise ValueError(
                    f"Merged commit verification failed: {error_name}"
                ) from error
            stdout = result.stdout
            return stdout.encode() if isinstance(stdout, str) else stdout

        commit = request.merged_commit_sha
        base_branch = self.settings.github_base_branch
        git("check-ref-format", "--branch", base_branch)
        try:
            origin_url = git("remote", "get-url", "origin").decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ValueError("Git origin fetch URL is not valid UTF-8") from error
        origin_repository = _github_repository_from_remote_url(origin_url)
        if (
            origin_repository is None
            or origin_repository.casefold()
            != self.settings.github_repository.strip().casefold()
        ):
            raise ValueError(
                "Git origin fetch URL does not match the configured GitHub repository"
            )
        trusted_base_ref = f"refs/remotes/origin/{base_branch}"
        git(
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            f"+refs/heads/{base_branch}:{trusted_base_ref}",
            timeout=60,
        )
        git("cat-file", "-e", f"{trusted_base_ref}^{{commit}}")
        try:
            trusted_base_tip = (
                git("rev-parse", "--verify", f"{trusted_base_ref}^{{commit}}")
                .decode("ascii")
                .strip()
            )
            requested_commit = (
                git("rev-parse", "--verify", f"{commit}^{{commit}}")
                .decode("ascii")
                .strip()
            )
        except UnicodeDecodeError as error:
            raise ValueError("Verified commit id is not valid ASCII") from error
        for resolved_commit in (trusted_base_tip, requested_commit):
            if len(resolved_commit) not in {40, 64} or any(
                character not in "0123456789abcdefABCDEF"
                for character in resolved_commit
            ):
                raise ValueError("Git did not return a canonical full commit id")
        if requested_commit.casefold() != trusted_base_tip.casefold():
            raise ValueError(
                "Merged commit must exactly match the fetched origin/base tip"
            )
        verified_commit = trusted_base_tip

        stored_patch = case.patch.content.encode("utf-8")
        stored_hash = hashlib.sha256(stored_patch).hexdigest()
        if stored_hash != case.patch.sha256.casefold():
            raise ValueError("Stored patch artifact hash does not match its content")
        merged_patch = git("show", f"{verified_commit}:{stored_patch_path.as_posix()}")
        if (
            hashlib.sha256(merged_patch).hexdigest() != case.patch.sha256.casefold()
            or merged_patch != stored_patch
        ):
            raise ValueError("Merged commit does not contain the exact validated patch artifact")

        context = self._context(case.asset_urn)
        if (
            context.source != "DATAHUB_MCP"
            or context.integration.status is not IntegrationStatus.SUCCEEDED
        ):
            raise ValueError(
                "Fresh live DataHub context is required for post-deploy verification"
            )
        proposal = CandidateProposal(
            id=case.selected_candidate.id,
            source_field=case.selected_candidate.source_field,
            target_alias=case.selected_candidate.target_alias,
            rationale=case.selected_candidate.rationale,
            semantic_evidence=case.selected_candidate.evidence_refs,
        )
        self.settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"verify-{case.id.casefold()}-",
            dir=self.settings.runtime_dir,
            ignore_cleanup_errors=True,
        ) as temporary_root:
            checkout = Path(temporary_root) / "checkout"
            worktree_added = False
            try:
                git(
                    "worktree",
                    "add",
                    "--detach",
                    str(checkout),
                    verified_commit,
                    timeout=60,
                )
                worktree_added = True
                binder = getattr(self.executor, "bind_to_checkout", None)
                if not callable(binder):
                    raise ValueError(
                        "Live evidence executor cannot bind to a verified git checkout"
                    )
                verified_executor = binder(
                    checkout_root=checkout,
                    repository_root=repo,
                )
                if not _produces_live_evidence(verified_executor):
                    raise ValueError(
                        "Verified-checkout executor does not produce live evidence"
                    )
                try:
                    execution = verified_executor.execute(
                        case_id=case.id, proposal=proposal, context=context
                    )
                except Exception as error:
                    raise ValueError(
                        "Post-deploy live evidence execution failed: "
                        f"{type(error).__name__}"
                    ) from error
            finally:
                if worktree_added:
                    _cleanup_worktree(repo, checkout)
        return execution, context.lineage_current, verified_commit

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
    mode = settings.execution_mode.strip().casefold()
    if mode == "postgres":
        if not settings.postgres_dsn or not settings.postgres_dsn.strip():
            raise ValueError(
                "DATARESCUE_POSTGRES_DSN is required when execution mode is postgres"
            )
        return PostgresDbtExecutor(
            postgres_dsn=settings.postgres_dsn,
            dbt_project_dir=settings.dbt_project_dir,
            dbt_profiles_dir=settings.dbt_profiles_dir,
            dbt_target=settings.dbt_target,
            evidence_dir=settings.runtime_dir / "evidence",
        )
    if mode == "replay":
        return ReplayEvidenceExecutor()
    raise ValueError(f"Unsupported execution mode: {settings.execution_mode!r}")


def _produces_live_evidence(executor: EvidenceExecutor) -> bool:
    return getattr(executor, "produces_live_evidence", False) is True


def _cleanup_worktree(repository: Path, checkout: Path) -> None:
    for arguments in (
        ("worktree", "remove", "--force", str(checkout)),
        ("worktree", "prune"),
    ):
        try:
            subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            # Verification outcome must not be rewritten by best-effort cleanup.
            continue


def _detection_payload(event: SchemaChangeEvent, case_id: str) -> dict[str, object]:
    return {
        "schema_change": event.model_dump(mode="json"),
        "incident_urn": f"urn:li:dataRescueIncident:{case_id}",
    }


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
