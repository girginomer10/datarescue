from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest

from scripts.seed_datahub_context import (
    SemanticContextSpec,
    VerificationError,
    main,
    seed_context,
    verify_context,
)


class FakeAspectStore:
    def __init__(self, spec: SemanticContextSpec, *, datasets_exist: bool = True) -> None:
        self.entities = (
            {
                spec.source_urn,
                spec.dbt_source_urn,
                spec.staging_urn,
                spec.staging_target_urn,
                spec.mart_urn,
            }
            if datasets_exist
            else set()
        )
        self.aspects: dict[tuple[str, str], dict[str, Any]] = (
            {
                (spec.dbt_source_urn, "upstreamLineage"): {
                    "upstreams": [{"dataset": spec.source_urn, "type": "COPY"}]
                },
                (spec.staging_urn, "upstreamLineage"): {
                    "upstreams": [
                        {"dataset": spec.dbt_source_urn, "type": "TRANSFORMED"}
                    ]
                },
                (spec.staging_target_urn, "upstreamLineage"): {
                    "upstreams": [{"dataset": spec.staging_urn, "type": "COPY"}]
                },
                (spec.mart_urn, "upstreamLineage"): {
                    "upstreams": [
                        {"dataset": spec.staging_target_urn, "type": "TRANSFORMED"}
                    ]
                },
            }
            if datasets_exist
            else {}
        )
        self.emissions: list[tuple[str, str, dict[str, Any]]] = []
        self.connection_tests = 0

    def test_connection(self) -> None:
        self.connection_tests += 1

    def entity_exists(self, urn: str) -> bool:
        return urn in self.entities or any(key[0] == urn for key in self.aspects)

    def get_aspect(self, urn: str, aspect_name: str) -> dict[str, Any] | None:
        value = self.aspects.get((urn, aspect_name))
        return copy.deepcopy(value) if value is not None else None

    def emit_aspect(
        self, urn: str, aspect_name: str, value: Mapping[str, Any]
    ) -> None:
        copied = copy.deepcopy(dict(value))
        self.aspects[(urn, aspect_name)] = copied
        self.entities.add(urn)
        self.emissions.append((urn, aspect_name, copied))

    def get_related_documents(self, asset_urn: str) -> list[dict[str, Any]]:
        related: list[dict[str, Any]] = []
        for (urn, aspect_name), value in self.aspects.items():
            if aspect_name != "documentInfo":
                continue
            links = value.get("relatedAssets", [])
            if not any(item.get("asset") == asset_urn for item in links):
                continue
            related.append({"urn": urn, "info": {"title": value.get("title")}})
        return related


def test_seed_is_idempotent_and_verifies_exact_context() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)

    changed = seed_context(store, spec, now_ms=123)
    first_emission_count = len(store.emissions)

    assert len(changed) == 14
    assert verify_context(store, spec)["status"] == "VERIFIED"
    assert seed_context(store, spec, now_ms=999) == []
    assert len(store.emissions) == first_emission_count


def test_seed_preserves_unrelated_attachments_and_fine_grained_lineage() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)
    other_owner = {"owner": "urn:li:corpuser:someone-else", "type": "DATA_STEWARD"}
    other_term = {"urn": "urn:li:glossaryTerm:GrossRevenue", "actor": spec.owner_urn}
    other_doc = {
        "url": "https://example.invalid/runbook",
        "description": "Existing runbook",
        "createStamp": {"time": 1, "actor": spec.owner_urn},
    }
    store.aspects[(spec.source_urn, "ownership")] = {"owners": [other_owner]}
    store.aspects[(spec.source_urn, "glossaryTerms")] = {
        "terms": [other_term],
        "auditStamp": {"time": 1, "actor": spec.owner_urn},
    }
    store.aspects[(spec.source_urn, "institutionalMemory")] = {"elements": [other_doc]}
    staging_lineage = store.aspects[(spec.staging_urn, "upstreamLineage")]
    staging_lineage["fineGrainedLineages"] = [
        {"upstreams": ["legacy"], "downstreams": ["current"]}
    ]
    lineage_before = copy.deepcopy(staging_lineage)

    seed_context(store, spec, now_ms=123)

    assert other_owner in store.aspects[(spec.source_urn, "ownership")]["owners"]
    assert other_term in store.aspects[(spec.source_urn, "glossaryTerms")]["terms"]
    assert other_doc in store.aspects[(spec.source_urn, "institutionalMemory")]["elements"]
    assert store.aspects[(spec.staging_urn, "upstreamLineage")] == lineage_before
    assert all(aspect_name != "upstreamLineage" for _, aspect_name, _ in store.emissions)


def test_document_update_preserves_unrelated_document_metadata() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)
    seed_context(store, spec, now_ms=123)
    info = store.aspects[(spec.context_document_urn, "documentInfo")]
    other_asset = {
        "asset": "urn:li:dataset:(urn:li:dataPlatform:postgres,other.table,PROD)"
    }
    other_document = {"document": "urn:li:document:other-context"}
    info["title"] = "stale title"
    info["customProperties"] = {"external": "preserved"}
    info["relatedAssets"].append(other_asset)
    info["relatedDocuments"] = [other_document]
    store.aspects[(spec.context_document_urn, "ownership")]["owners"].append(
        {"owner": "urn:li:corpuser:other", "type": "DATA_STEWARD"}
    )

    changed = seed_context(store, spec, now_ms=456)

    updated = store.aspects[(spec.context_document_urn, "documentInfo")]
    assert f"{spec.context_document_urn}:documentInfo" in changed
    assert updated["customProperties"] == {"external": "preserved"}
    assert other_asset in updated["relatedAssets"]
    assert other_document in updated["relatedDocuments"]
    assert {"owner": "urn:li:corpuser:other", "type": "DATA_STEWARD"} in store.aspects[
        (spec.context_document_urn, "ownership")
    ]["owners"]


def test_seed_preserves_unknown_fields_while_repairing_managed_metadata() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)
    store.aspects[(spec.term_urn, "glossaryTermInfo")] = {
        "definition": "stale definition",
        "termSource": "INTERNAL",
        "name": "NetRevenue",
        "customProperties": {
            "externalCatalogId": "term-42",
            "managedBy": "legacy-agent",
        },
    }
    store.aspects[(spec.source_urn, "ownership")] = {
        "owners": [
            {
                "owner": spec.owner_urn,
                "type": "DATA_STEWARD",
                "typeUrn": "urn:li:ownershipType:finance-primary",
            }
        ]
    }
    store.aspects[(spec.source_urn, "glossaryTerms")] = {
        "terms": [
            {
                "urn": spec.term_urn,
                "actor": "urn:li:corpuser:legacy-agent",
                "context": "manually-curated",
            }
        ]
    }

    seed_context(store, spec, now_ms=456)
    emission_count = len(store.emissions)

    term_info = store.aspects[(spec.term_urn, "glossaryTermInfo")]
    assert term_info["definition"] == spec.term_definition
    assert term_info["customProperties"] == {
        "externalCatalogId": "term-42",
        "managedBy": "DataRescue",
    }
    owner = store.aspects[(spec.source_urn, "ownership")]["owners"][0]
    assert owner == {
        "owner": spec.owner_urn,
        "type": "DATAOWNER",
        "typeUrn": "urn:li:ownershipType:finance-primary",
    }
    term = store.aspects[(spec.source_urn, "glossaryTerms")]["terms"][0]
    assert term == {
        "urn": spec.term_urn,
        "actor": spec.owner_urn,
        "context": "manually-curated",
    }
    assert seed_context(store, spec, now_ms=789) == []
    assert len(store.emissions) == emission_count


def test_verify_requires_reverse_dataset_document_relationship() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)
    seed_context(store, spec, now_ms=123)
    store.aspects[(spec.context_document_urn, "documentInfo")]["relatedAssets"] = []

    with pytest.raises(VerificationError, match="does not expose the deterministic"):
        verify_context(store, spec)


def test_seed_fails_closed_instead_of_rewriting_dbt_lineage() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)
    store.aspects[(spec.staging_urn, "upstreamLineage")] = {"upstreams": []}

    with pytest.raises(VerificationError, match="dbt-source-to-staging lineage"):
        seed_context(store, spec, now_ms=123)

    assert store.emissions == []


@pytest.mark.parametrize(
    "bridge_lineage",
    [
        None,
        {"upstreams": [{"dataset": "urn:li:dataset:wrong", "type": "COPY"}]},
        {"upstreams": [{"dataset": SemanticContextSpec().staging_urn, "type": "VIEW"}]},
    ],
)
def test_seed_requires_the_dbt_materialization_bridge_lineage(
    bridge_lineage: dict[str, Any] | None,
) -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)
    if bridge_lineage is None:
        store.aspects.pop((spec.staging_target_urn, "upstreamLineage"))
    else:
        store.aspects[(spec.staging_target_urn, "upstreamLineage")] = bridge_lineage

    with pytest.raises(
        VerificationError, match="dbt-staging-to-materialized-staging lineage"
    ):
        seed_context(store, spec, now_ms=123)

    assert store.emissions == []


def test_verify_fails_closed_when_context_is_missing() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)

    with pytest.raises(VerificationError, match="glossary definition"):
        verify_context(store, spec)


def test_seed_requires_ingested_datasets() -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec, datasets_exist=False)

    with pytest.raises(VerificationError, match="Run the PostgreSQL and dbt ingestion"):
        seed_context(store, spec, now_ms=123)

    assert store.emissions == []


def test_verify_cli_exits_nonzero_without_required_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = SemanticContextSpec()
    store = FakeAspectStore(spec)

    result = main(
        ["verify", "--gms-url", "http://datahub.test"],
        store_factory=lambda **_: store,
    )

    assert result == 1
    assert "verification failed" in capsys.readouterr().err
