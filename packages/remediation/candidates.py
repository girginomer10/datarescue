from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from pydantic import BaseModel

from apps.api.models import (
    CandidateProposal,
    ContextBundle,
    IntegrationResult,
    IntegrationStatus,
    SchemaChangeEvent,
)
from packages.remediation.sql_safety import (
    SQLSafetyError,
    require_identifier,
    require_revenue_alias,
)


class CandidateGenerationResult(BaseModel):
    candidates: list[CandidateProposal]
    integration: IntegrationResult


class CandidateGenerator:
    """Propose mappings; it never accepts or deploys a candidate."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-5.6-terra",
        base_url: str = "https://api.openai.com/v1",
        replay_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.replay_mode = replay_mode
        self.transport = transport

    def propose(
        self, event: SchemaChangeEvent, context: ContextBundle
    ) -> CandidateGenerationResult:
        if not self.api_key:
            candidates = self._deterministic_candidates(event, context)
            status = (
                IntegrationStatus.RECORDED_REPLAY
                if self.replay_mode
                else IntegrationStatus.NOT_CONFIGURED
            )
            return CandidateGenerationResult(
                candidates=candidates,
                integration=IntegrationResult(
                    status=status,
                    operation="candidate_generation",
                    message=(
                        "Recorded replay candidates loaded; deterministic policy "
                        "still owns the decision"
                        if self.replay_mode
                        else "OPENAI_API_KEY is not configured; deterministic candidates used"
                    ),
                    evidence_refs=["artifact://replay/candidate-proposals.json"]
                    if self.replay_mode
                    else [],
                    details={"model": None, "decision_authority": "deterministic_policy"},
                ),
            )
        try:
            return self._openai_candidates(event, context)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            fallback = self._deterministic_candidates(event, context)
            return CandidateGenerationResult(
                candidates=fallback,
                integration=IntegrationResult(
                    status=IntegrationStatus.FAILED,
                    operation="candidate_generation",
                    message=(
                        "OpenAI candidate generation failed; safe deterministic "
                        f"fallback used: {error}"
                    ),
                    details={"model": self.model, "decision_authority": "deterministic_policy"},
                ),
            )

    def _openai_candidates(
        self, event: SchemaChangeEvent, context: ContextBundle
    ) -> CandidateGenerationResult:
        schema = {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_field": {"type": "string"},
                            "target_alias": {"type": "string"},
                            "rationale": {"type": "string"},
                            "semantic_evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "source_field",
                            "target_alias",
                            "rationale",
                            "semantic_evidence",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["candidates"],
            "additionalProperties": False,
        }
        prompt = {
            "instruction": (
                "Propose identifier-only source-to-alias mappings for a broken dbt model. "
                "Do not emit SQL. Cite only supplied evidence identifiers. The candidates "
                "will be executed and accepted or rejected by deterministic gates."
            ),
            "schema_change": event.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
        }
        with httpx.Client(timeout=30, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "reasoning": {"effort": "medium"},
                    "input": json.dumps(prompt),
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "datarescue_candidates",
                            "strict": True,
                            "schema": schema,
                        }
                    },
                },
            )
            response.raise_for_status()
            body = response.json()
        parsed = json.loads(_response_output_text(body))
        after_names = {field.name for field in event.after_fields}
        candidates: list[CandidateProposal] = []
        for index, raw in enumerate(parsed["candidates"]):
            source_field = require_identifier(str(raw["source_field"]), "source field")
            target_alias = require_revenue_alias(str(raw["target_alias"]))
            if source_field not in after_names:
                raise SQLSafetyError(
                    f"Candidate field {source_field!r} is absent from current schema"
                )
            evidence_refs = [str(ref) for ref in raw["semantic_evidence"]]
            allowed_refs = {
                event.entity_urn,
                *context.lineage_urns,
                *context.context_documents,
            }
            hallucinated = sorted(set(evidence_refs) - allowed_refs)
            if hallucinated:
                raise SQLSafetyError(
                    "Candidate cited evidence absent from the fetched context: "
                    + ", ".join(hallucinated)
                )
            candidates.append(
                CandidateProposal(
                    id=_candidate_id(source_field, index),
                    source_field=source_field,
                    target_alias=target_alias,
                    rationale=str(raw["rationale"]),
                    semantic_evidence=evidence_refs,
                )
            )
        return CandidateGenerationResult(
            candidates=candidates,
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="candidate_generation",
                message="OpenAI proposed structured mappings; no acceptance decision was delegated",
                details={"model": self.model, "decision_authority": "deterministic_policy"},
            ),
        )

    @staticmethod
    def _deterministic_candidates(
        event: SchemaChangeEvent, context: ContextBundle
    ) -> list[CandidateProposal]:
        fields = {field.name for field in event.after_fields}
        candidates: list[CandidateProposal] = []
        evidence = context.context_documents or ["urn:li:glossaryTerm:NetRevenue"]
        preferred = ["gross_amount", "net_amount"]
        ordered_fields = [name for name in preferred if name in fields]
        ordered_fields.extend(sorted(fields - set(ordered_fields) - {"payment_id"}))
        for index, field in enumerate(ordered_fields):
            if field == "gross_amount":
                rationale = "Candidate compiles but conflicts with recognized net revenue semantics"
            elif field == "net_amount":
                rationale = "Matches the glossary definition of recognized net settled revenue"
            else:
                rationale = (
                    "Schema-compatible candidate requiring deterministic evidence validation"
                )
            candidates.append(
                CandidateProposal(
                    id=_candidate_id(field, index),
                    source_field=field,
                    target_alias="revenue",
                    rationale=rationale,
                    semantic_evidence=evidence,
                )
            )
        return candidates


def _candidate_id(source_field: str, index: int) -> str:
    digest = hashlib.sha256(f"{index}:{source_field}".encode()).hexdigest()[:8]
    return f"candidate-{source_field}-{digest}"


def _response_output_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str):
        return str(body["output_text"])
    for item in body.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return str(content["text"])
    raise ValueError("Responses API returned no structured output text")
