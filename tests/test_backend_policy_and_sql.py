from __future__ import annotations

import json

import httpx
import pytest

from apps.api.models import (
    BuildResult,
    CandidateProposal,
    ContextBundle,
    IntegrationResult,
    IntegrationStatus,
    ReconciliationMetrics,
    SchemaChangeEvent,
    SchemaField,
    SemanticVerdict,
)
from packages.policy import PolicyEngine
from packages.remediation.candidates import CandidateGenerator
from packages.remediation.sql_safety import (
    SQLSafetyError,
    render_candidate_sql,
    render_dbt_patch,
)
from tests.backend_helpers import REPO_ROOT


def test_policy_rejects_compiling_but_semantically_wrong_candidate() -> None:
    decision = PolicyEngine().evaluate(
        semantic_verdict=SemanticVerdict.CONFLICT,
        metrics=ReconciliationMetrics(
            total_variance_pct=3.40,
            row_count_variance_pct=0.0,
            primary_key_overlap_pct=100.0,
            null_rate_delta_percentage_points=0.0,
        ),
        build=BuildResult(passed=True, passed_checks=8, total_checks=8),
        lineage_current=True,
    )

    assert decision.accepted is False
    assert {check.name for check in decision.checks if not check.passed} == {
        "semantic_evidence",
        "total_variance",
    }


def test_policy_accepts_net_candidate_at_exact_safety_boundaries() -> None:
    decision = PolicyEngine().evaluate(
        semantic_verdict=SemanticVerdict.MATCH,
        metrics=ReconciliationMetrics(
            total_variance_pct=0.50,
            row_count_variance_pct=0.10,
            primary_key_overlap_pct=99.90,
            null_rate_delta_percentage_points=0.50,
        ),
        build=BuildResult(passed=True, passed_checks=8, total_checks=8),
        lineage_current=True,
    )

    assert decision.accepted is True


@pytest.mark.parametrize(
    ("source_field", "target_alias"),
    [
        ("net_amount; DROP TABLE payments_raw", "revenue"),
        ("net_amount", "attacker_controlled_alias"),
        ("net_amount --", "revenue"),
    ],
)
def test_candidate_renderer_rejects_injection_and_non_revenue_alias(
    source_field: str, target_alias: str
) -> None:
    proposal = CandidateProposal(
        id="candidate-malicious",
        source_field=source_field,
        target_alias=target_alias,
        rationale="untrusted metadata",
    )

    with pytest.raises(SQLSafetyError):
        render_candidate_sql(proposal, relation="raw.payments_raw")


def test_dbt_patch_changes_only_allowlisted_default_and_preserves_model() -> None:
    model_path = REPO_ROOT / "demo/dbt/models/staging/stg_payments.sql"
    existing = model_path.read_text(encoding="utf-8")
    proposal = CandidateProposal(
        id="candidate-net",
        source_field="net_amount",
        target_alias="revenue",
        rationale="matches glossary",
    )

    patched = render_dbt_patch(proposal, existing_sql=existing)

    assert 'env_var("DATARESCUE_REVENUE_COLUMN", "net_amount")' in patched
    assert 'env_var("DATARESCUE_REVENUE_COLUMN", "amount")' not in patched
    for preserved in ("customer_id", "paid_at", "currency", "status", "adapter.quote"):
        assert preserved in patched
    assert len(patched.splitlines()) == len(existing.splitlines())


def test_dbt_patch_is_idempotent_for_the_validated_mapping() -> None:
    model_path = REPO_ROOT / "demo/dbt/models/staging/stg_payments.sql"
    healthy = model_path.read_text(encoding="utf-8")
    already_applied = healthy.replace(
        'env_var("DATARESCUE_REVENUE_COLUMN", "amount")',
        'env_var("DATARESCUE_REVENUE_COLUMN", "net_amount")',
    )
    proposal = CandidateProposal(
        id="candidate-net",
        source_field="net_amount",
        target_alias="revenue",
        rationale="matches glossary",
    )

    assert render_dbt_patch(proposal, existing_sql=already_applied) == already_applied


def test_model_evidence_references_must_exist_in_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-5.6-terra"
        assert payload["reasoning"] == {"effort": "medium"}
        output_format = payload["text"]["format"]
        assert output_format["type"] == "json_schema"
        assert output_format["strict"] is True
        assert output_format["schema"]["additionalProperties"] is False
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "candidates": [
                            {
                                "source_field": "net_amount",
                                "target_alias": "revenue",
                                "rationale": "looks plausible",
                                "semantic_evidence": ["urn:li:glossaryTerm:Hallucinated"],
                            }
                        ]
                    }
                )
            },
        )

    generator = CandidateGenerator(
        api_key="test-key",
        base_url="https://api.test/v1",
        transport=httpx.MockTransport(handler),
    )
    event = SchemaChangeEvent(
        entity_urn="urn:li:dataset:test",
        before_fields=[SchemaField(name="amount")],
        after_fields=[SchemaField(name="net_amount")],
    )
    context = ContextBundle(
        asset_urn=event.entity_urn,
        glossary_definition="Revenue is net amount",
        lineage_current=True,
        context_documents=["urn:li:glossaryTerm:NetRevenue"],
        source="test",
        integration=IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="test",
            message="test",
        ),
    )

    result = generator.propose(event, context)

    assert result.integration.status is IntegrationStatus.FAILED
    assert all(
        "Hallucinated" not in ref
        for candidate in result.candidates
        for ref in candidate.semantic_evidence
    )
