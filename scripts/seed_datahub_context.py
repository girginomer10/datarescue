#!/usr/bin/env python3
"""Seed and verify the semantic context required by the connected demo.

The repository intentionally does not install the large DataHub SDK in its base
environment. Run this script with the same pinned SDK version as the demo:

    uv run --python 3.11 --with 'acryl-datahub==1.6.0' \
      python scripts/seed_datahub_context.py seed

Only ``DATAHUB_GMS_URL`` is required for a local unauthenticated quickstart.
``DATAHUB_TOKEN`` (or ``DATARESCUE_DATAHUB_TOKEN``) is forwarded when the GMS
instance requires authentication.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

SOURCE_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)"
)
DBT_SOURCE_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.raw.payments_raw,PROD)"
)
STAGING_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.stg_payments,PROD)"
)
STAGING_TARGET_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.analytics.stg_payments,PROD)"
)
MART_ASSET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.fct_revenue,PROD)"
)
TERM_URN = "urn:li:glossaryTerm:NetRevenue"
OWNER_URN = "urn:li:corpuser:finance-data"

TERM_DEFINITION = (
    "Recognized merchant revenue after processing fees. For the DataRescue payment "
    "contract this is the legacy amount value and maps to net_amount after the split; "
    "gross_amount must not be used."
)
ASSET_DESCRIPTION = (
    "Payment source monitored by DataRescue. Recognized revenue is net of processing "
    "fees, so the legacy amount contract maps to net_amount after schema drift."
)
DOCUMENT_URL = (
    "https://github.com/girginomer10/datarescue/blob/main/docs/devpost-submission.md"
)
DOCUMENT_DESCRIPTION = "DataRescue recognized-revenue contract and recovery evidence guide."
SHARED_DOCUMENT_URN = "urn:li:document:__system_shared_documents"
CONTEXT_DOCUMENT_URN = "urn:li:document:datarescue-net-revenue-contract"
CONTEXT_DOCUMENT_TITLE = "DataRescue Net Revenue Contract"
CONTEXT_DOCUMENT_CONTENT = f"""# DataRescue Net Revenue Contract

## Canonical asset

`{SOURCE_ASSET_URN}`

## Semantic rule

Recognized merchant revenue is net of processing fees. After the legacy `amount`
column is split, DataRescue must map the contract to `net_amount`. A candidate that
uses `gross_amount` is unsafe even when it compiles and passes structural dbt tests.

## Recovery evidence

- Glossary term: `{TERM_URN}`
- Deterministic owner: `{OWNER_URN}`
- Required decision: select `net_amount`; reject fee-inclusive `gross_amount`
- Closure rule: keep the incident active until the merged commit passes dbt build
  and reconciliation again.
"""


class VerificationError(RuntimeError):
    """Raised when the connected DataHub state does not match the demo contract."""


class AspectStore(Protocol):
    def test_connection(self) -> None: ...

    def entity_exists(self, urn: str) -> bool: ...

    def get_aspect(self, urn: str, aspect_name: str) -> dict[str, Any] | None: ...

    def emit_aspect(self, urn: str, aspect_name: str, value: Mapping[str, Any]) -> None: ...

    def get_related_documents(self, asset_urn: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SemanticContextSpec:
    source_urn: str = SOURCE_ASSET_URN
    dbt_source_urn: str = DBT_SOURCE_ASSET_URN
    staging_urn: str = STAGING_ASSET_URN
    staging_target_urn: str = STAGING_TARGET_ASSET_URN
    mart_urn: str = MART_ASSET_URN
    term_urn: str = TERM_URN
    owner_urn: str = OWNER_URN
    term_definition: str = TERM_DEFINITION
    asset_description: str = ASSET_DESCRIPTION
    document_url: str = DOCUMENT_URL
    document_description: str = DOCUMENT_DESCRIPTION
    shared_document_urn: str = SHARED_DOCUMENT_URN
    context_document_urn: str = CONTEXT_DOCUMENT_URN
    context_document_title: str = CONTEXT_DOCUMENT_TITLE
    context_document_content: str = CONTEXT_DOCUMENT_CONTENT


class SdkAspectStore:
    """Thin facade over the DataHub 1.6 Python SDK's stable graph/emitter APIs."""

    def __init__(self, *, gms_url: str, token: str | None) -> None:
        try:
            mcp_module = importlib.import_module("datahub.emitter.mcp")
            emitter_module = importlib.import_module("datahub.emitter.rest_emitter")
            graph_module = importlib.import_module("datahub.ingestion.graph.client")
            schema_classes = importlib.import_module("datahub.metadata.schema_classes")
        except ImportError as error:  # pragma: no cover - exercised by the CLI environment
            raise RuntimeError(
                "The DataHub SDK is required. Run with "
                "`uv run --python 3.11 --with 'acryl-datahub==1.6.0' python "
                "scripts/seed_datahub_context.py ...`."
            ) from error

        self._mcp_wrapper = mcp_module.MetadataChangeProposalWrapper
        self._emitter = emitter_module.DatahubRestEmitter(gms_server=gms_url, token=token)
        config = graph_module.DatahubClientConfig(server=gms_url, token=token)
        self._graph = graph_module.DataHubGraph(config)
        self._aspect_classes = {
            "corpUserInfo": schema_classes.CorpUserInfoClass,
            "documentInfo": schema_classes.DocumentInfoClass,
            "documentSettings": schema_classes.DocumentSettingsClass,
            "editableDatasetProperties": schema_classes.EditableDatasetPropertiesClass,
            "glossaryTermInfo": schema_classes.GlossaryTermInfoClass,
            "glossaryTerms": schema_classes.GlossaryTermsClass,
            "institutionalMemory": schema_classes.InstitutionalMemoryClass,
            "ownership": schema_classes.OwnershipClass,
            "subTypes": schema_classes.SubTypesClass,
            "upstreamLineage": schema_classes.UpstreamLineageClass,
        }

    def test_connection(self) -> None:
        self._emitter.test_connection()

    def entity_exists(self, urn: str) -> bool:
        return bool(self._graph.exists(urn))

    def get_aspect(self, urn: str, aspect_name: str) -> dict[str, Any] | None:
        aspect_class = self._aspect_class(aspect_name)
        value = self._graph.get_aspect(urn, aspect_class)
        return dict(value.to_obj()) if value is not None else None

    def emit_aspect(self, urn: str, aspect_name: str, value: Mapping[str, Any]) -> None:
        aspect_class = self._aspect_class(aspect_name)
        aspect = aspect_class.from_obj(copy.deepcopy(dict(value)))
        self._emitter.emit(self._mcp_wrapper(entityUrn=urn, aspect=aspect))

    def get_related_documents(self, asset_urn: str) -> list[dict[str, Any]]:
        query = """
        query GetRelatedDocuments($urn: String!, $input: RelatedDocumentsInput!) {
          entity(urn: $urn) {
            ... on Dataset {
              relatedDocuments(input: $input) {
                total
                documents { urn info { title } }
              }
            }
          }
        }
        """
        result = self._graph.execute_graphql(
            query,
            variables={"urn": asset_urn, "input": {"start": 0, "count": 100}},
            operation_name="GetRelatedDocuments",
        )
        entity = result.get("entity")
        if not isinstance(entity, Mapping):
            raise ValueError(f"DataHub returned no entity for related-document query: {asset_urn}")
        related = entity.get("relatedDocuments")
        if not isinstance(related, Mapping):
            raise ValueError(f"DataHub returned no relatedDocuments result for: {asset_urn}")
        documents = related.get("documents")
        if not isinstance(documents, list):
            raise ValueError(f"DataHub returned malformed related documents for: {asset_urn}")
        return [dict(item) for item in documents if isinstance(item, Mapping)]

    def _aspect_class(self, aspect_name: str) -> Any:
        try:
            return self._aspect_classes[aspect_name]
        except KeyError as error:
            raise ValueError(f"Unsupported DataHub aspect: {aspect_name}") from error


def seed_context(
    store: AspectStore,
    spec: SemanticContextSpec,
    *,
    now_ms: int | None = None,
) -> list[str]:
    """Upsert semantic context without deleting unrelated metadata."""

    store.test_connection()
    _require_ingested_datasets(store, spec)
    lineage_errors: list[str] = []
    _check_dbt_lineage(store, spec, lineage_errors)
    if lineage_errors:
        raise VerificationError(
            "DataHub dbt-lineage verification failed:\n- " + "\n- ".join(lineage_errors)
        )
    stamp = {
        "time": now_ms if now_ms is not None else int(time.time() * 1000),
        "actor": spec.owner_urn,
    }
    changed: list[str] = []

    _ensure_object_aspect(
        store,
        spec.term_urn,
        "glossaryTermInfo",
        {
            "definition": spec.term_definition,
            "termSource": "INTERNAL",
            "name": "NetRevenue",
            "customProperties": {"managedBy": "DataRescue"},
        },
        changed,
    )
    _ensure_object_aspect(
        store,
        spec.owner_urn,
        "corpUserInfo",
        {
            "active": True,
            "displayName": "Finance Data",
            "title": "Finance Data Owner",
            "email": "finance-data@example.invalid",
            "system": False,
        },
        changed,
    )
    _ensure_description(store, spec, stamp, changed)
    _ensure_owner(store, spec, stamp, changed)
    _ensure_term_attachment(store, spec, stamp, changed)
    _ensure_document(store, spec, stamp, changed)
    _ensure_related_datahub_document(store, spec, stamp, changed)
    # dbt owns the lineage aspects. Rewriting them here would discard provenance
    # and could race the next ingestion, so verification below is deliberately
    # fail-closed instead of creating a synthetic shortcut edge.
    verify_context(store, spec, test_connection=False)
    return changed


def verify_context(
    store: AspectStore,
    spec: SemanticContextSpec,
    *,
    test_connection: bool = True,
) -> dict[str, Any]:
    """Assert the exact URNs and semantic attachments used by DataRescue."""

    if test_connection:
        store.test_connection()
    errors: list[str] = []

    dataset_urns = (
        spec.source_urn,
        spec.dbt_source_urn,
        spec.staging_urn,
        spec.staging_target_urn,
        spec.mart_urn,
    )
    for urn in dataset_urns:
        if not store.entity_exists(urn):
            errors.append(f"missing ingested dataset: {urn}")

    _require_fields(
        store.get_aspect(spec.term_urn, "glossaryTermInfo"),
        {"definition": spec.term_definition, "termSource": "INTERNAL", "name": "NetRevenue"},
        "NetRevenue glossary definition",
        errors,
    )
    _require_fields(
        store.get_aspect(spec.owner_urn, "corpUserInfo"),
        {"active": True, "displayName": "Finance Data", "title": "Finance Data Owner"},
        "Finance Data owner",
        errors,
    )
    _require_fields(
        store.get_aspect(spec.source_urn, "editableDatasetProperties"),
        {"description": spec.asset_description},
        "source description",
        errors,
    )
    _require_list_entry(
        store.get_aspect(spec.source_urn, "ownership"),
        "owners",
        {"owner": spec.owner_urn, "type": "DATAOWNER"},
        "source owner attachment",
        errors,
    )
    _require_list_entry(
        store.get_aspect(spec.source_urn, "glossaryTerms"),
        "terms",
        {"urn": spec.term_urn, "actor": spec.owner_urn},
        "source glossary attachment",
        errors,
    )
    _require_list_entry(
        store.get_aspect(spec.source_urn, "institutionalMemory"),
        "elements",
        {"url": spec.document_url, "description": spec.document_description},
        "source documentation context",
        errors,
    )
    _check_related_datahub_document(store, spec, errors)
    _check_dbt_lineage(store, spec, errors)

    if errors:
        message = "DataHub semantic-context verification failed:\n- " + "\n- ".join(errors)
        raise VerificationError(message)
    return {
        "status": "VERIFIED",
        "source_urn": spec.source_urn,
        "term_urn": spec.term_urn,
        "owner_urn": spec.owner_urn,
        "lineage_urns": list(dataset_urns),
        "document_url": spec.document_url,
        "document_urn": spec.context_document_urn,
    }


def _require_ingested_datasets(store: AspectStore, spec: SemanticContextSpec) -> None:
    missing = [
        urn
        for urn in (
            spec.source_urn,
            spec.dbt_source_urn,
            spec.staging_urn,
            spec.staging_target_urn,
            spec.mart_urn,
        )
        if not store.entity_exists(urn)
    ]
    if missing:
        raise VerificationError(
            "Run the PostgreSQL and dbt ingestion before semantic seeding; missing:\n- "
            + "\n- ".join(missing)
        )


def _ensure_object_aspect(
    store: AspectStore,
    urn: str,
    aspect_name: str,
    required: Mapping[str, Any],
    changed: list[str],
) -> None:
    current = store.get_aspect(urn, aspect_name) or {}
    if _contains(current, required):
        return
    updated = _merge_required(current, required)
    store.emit_aspect(urn, aspect_name, updated)
    changed.append(f"{urn}:{aspect_name}")


def _ensure_description(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    aspect_name = "editableDatasetProperties"
    current = store.get_aspect(spec.source_urn, aspect_name) or {}
    if current.get("description") == spec.asset_description:
        return
    updated = copy.deepcopy(current)
    updated.setdefault("created", copy.deepcopy(dict(stamp)))
    updated["lastModified"] = copy.deepcopy(dict(stamp))
    updated["description"] = spec.asset_description
    store.emit_aspect(spec.source_urn, aspect_name, updated)
    changed.append(f"{spec.source_urn}:{aspect_name}")


def _ensure_owner(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    expected = {"owner": spec.owner_urn, "type": "DATAOWNER"}
    _ensure_list_aspect(
        store,
        spec.source_urn,
        "ownership",
        "owners",
        expected,
        identity_key="owner",
        stamp_key="lastModified",
        stamp=stamp,
        changed=changed,
    )


def _ensure_term_attachment(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    expected = {"urn": spec.term_urn, "actor": spec.owner_urn}
    _ensure_list_aspect(
        store,
        spec.source_urn,
        "glossaryTerms",
        "terms",
        expected,
        identity_key="urn",
        stamp_key="auditStamp",
        stamp=stamp,
        changed=changed,
    )


def _ensure_document(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    aspect_name = "institutionalMemory"
    current = store.get_aspect(spec.source_urn, aspect_name) or {}
    elements = [copy.deepcopy(item) for item in current.get("elements", [])]
    expected = {"url": spec.document_url, "description": spec.document_description}
    for index, element in enumerate(elements):
        if element.get("url") != spec.document_url:
            continue
        if _contains(element, expected):
            return
        create_stamp = element.get("createStamp") or copy.deepcopy(dict(stamp))
        elements[index] = {
            **element,
            **expected,
            "createStamp": create_stamp,
            "updateStamp": copy.deepcopy(dict(stamp)),
        }
        break
    else:
        elements.append(
            {**expected, "createStamp": copy.deepcopy(dict(stamp)), "updateStamp": stamp}
        )
    updated = copy.deepcopy(current)
    updated["elements"] = elements
    store.emit_aspect(spec.source_urn, aspect_name, updated)
    changed.append(f"{spec.source_urn}:{aspect_name}")


def _ensure_related_datahub_document(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    _ensure_shared_document_parent(store, spec, stamp, changed)
    _ensure_context_document_info(store, spec, stamp, changed)
    _ensure_subtype(store, spec.context_document_urn, "Context", changed)
    _ensure_document_settings(store, spec.context_document_urn, stamp, changed)
    _ensure_list_aspect(
        store,
        spec.context_document_urn,
        "ownership",
        "owners",
        {"owner": spec.owner_urn, "type": "DATAOWNER"},
        identity_key="owner",
        stamp_key="lastModified",
        stamp=stamp,
        changed=changed,
    )
    _ensure_list_aspect(
        store,
        spec.context_document_urn,
        "glossaryTerms",
        "terms",
        {"urn": spec.term_urn, "actor": spec.owner_urn},
        identity_key="urn",
        stamp_key="auditStamp",
        stamp=stamp,
        changed=changed,
    )


def _ensure_shared_document_parent(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    # Official MCP v0.6 save_document uses this fixed shared-folder URN and leaves
    # an existing folder untouched. Mirror that behavior to avoid overwriting a
    # catalog administrator's title, contents, ownership, or other metadata.
    if store.entity_exists(spec.shared_document_urn):
        return
    store.emit_aspect(
        spec.shared_document_urn,
        "documentInfo",
        {
            "title": "Shared",
            "source": {"sourceType": "NATIVE"},
            "status": {"state": "PUBLISHED"},
            "contents": {
                "text": "Contains shared documents authored through DataHub agents."
            },
            "created": copy.deepcopy(dict(stamp)),
            "lastModified": copy.deepcopy(dict(stamp)),
        },
    )
    changed.append(f"{spec.shared_document_urn}:documentInfo")
    _ensure_subtype(store, spec.shared_document_urn, "Folder", changed)
    _ensure_document_settings(store, spec.shared_document_urn, stamp, changed)


def _ensure_context_document_info(
    store: AspectStore,
    spec: SemanticContextSpec,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    aspect_name = "documentInfo"
    current = store.get_aspect(spec.context_document_urn, aspect_name) or {}
    expected = {
        "title": spec.context_document_title,
        "source": {"sourceType": "NATIVE"},
        "status": {"state": "PUBLISHED"},
        "contents": {"text": spec.context_document_content},
        "parentDocument": {"document": spec.shared_document_urn},
    }
    related_assets = [copy.deepcopy(item) for item in current.get("relatedAssets", [])]
    asset_link = {"asset": spec.source_urn}
    has_asset_link = any(_contains(item, asset_link) for item in related_assets)
    if _contains(current, expected) and has_asset_link:
        return
    if not has_asset_link:
        related_assets.append(asset_link)
    updated = copy.deepcopy(current)
    updated.update(copy.deepcopy(expected))
    updated.setdefault("customProperties", {})
    updated.setdefault("created", copy.deepcopy(dict(stamp)))
    updated["lastModified"] = copy.deepcopy(dict(stamp))
    updated["relatedAssets"] = related_assets
    store.emit_aspect(spec.context_document_urn, aspect_name, updated)
    changed.append(f"{spec.context_document_urn}:{aspect_name}")


def _ensure_subtype(
    store: AspectStore,
    document_urn: str,
    subtype: str,
    changed: list[str],
) -> None:
    aspect_name = "subTypes"
    current = store.get_aspect(document_urn, aspect_name) or {}
    type_names = list(current.get("typeNames", []))
    if subtype in type_names:
        return
    type_names.append(subtype)
    updated = copy.deepcopy(current)
    updated["typeNames"] = type_names
    store.emit_aspect(document_urn, aspect_name, updated)
    changed.append(f"{document_urn}:{aspect_name}")


def _ensure_document_settings(
    store: AspectStore,
    document_urn: str,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    aspect_name = "documentSettings"
    current = store.get_aspect(document_urn, aspect_name) or {}
    if current.get("showInGlobalContext") is True:
        return
    updated = copy.deepcopy(current)
    updated["showInGlobalContext"] = True
    updated["lastModified"] = copy.deepcopy(dict(stamp))
    store.emit_aspect(document_urn, aspect_name, updated)
    changed.append(f"{document_urn}:{aspect_name}")


def _ensure_list_aspect(
    store: AspectStore,
    urn: str,
    aspect_name: str,
    list_key: str,
    expected: Mapping[str, Any],
    *,
    identity_key: str,
    stamp_key: str | None,
    stamp: Mapping[str, Any],
    changed: list[str],
) -> None:
    current = store.get_aspect(urn, aspect_name) or {}
    entries = [copy.deepcopy(item) for item in current.get(list_key, [])]
    replacement = copy.deepcopy(dict(expected))
    for index, item in enumerate(entries):
        if item.get(identity_key) != expected[identity_key]:
            continue
        if _contains(item, expected):
            return
        entries[index] = _merge_required(item, expected)
        break
    else:
        entries.append(replacement)
    updated = copy.deepcopy(current)
    updated[list_key] = entries
    if stamp_key is not None:
        updated[stamp_key] = copy.deepcopy(dict(stamp))
    store.emit_aspect(urn, aspect_name, updated)
    changed.append(f"{urn}:{aspect_name}")


def _require_fields(
    actual: Mapping[str, Any] | None,
    expected: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    if actual is None or not _contains(actual, expected):
        errors.append(f"{label} is missing or does not match the exact contract")


def _check_dbt_lineage(
    store: AspectStore,
    spec: SemanticContextSpec,
    errors: list[str],
) -> None:
    checks = (
        (
            spec.dbt_source_urn,
            {"dataset": spec.source_urn, "type": "COPY"},
            "physical-source-to-dbt-source lineage",
        ),
        (
            spec.staging_urn,
            {"dataset": spec.dbt_source_urn, "type": "TRANSFORMED"},
            "dbt-source-to-staging lineage",
        ),
        (
            spec.staging_target_urn,
            {"dataset": spec.staging_urn, "type": "COPY"},
            "dbt-staging-to-materialized-staging lineage",
        ),
        (
            spec.mart_urn,
            {"dataset": spec.staging_target_urn, "type": "TRANSFORMED"},
            "materialized-staging-to-mart lineage",
        ),
    )
    for downstream_urn, expected, label in checks:
        _require_list_entry(
            store.get_aspect(downstream_urn, "upstreamLineage"),
            "upstreams",
            expected,
            label,
            errors,
        )


def _check_related_datahub_document(
    store: AspectStore,
    spec: SemanticContextSpec,
    errors: list[str],
) -> None:
    if not store.entity_exists(spec.shared_document_urn):
        errors.append(f"shared document parent is missing: {spec.shared_document_urn}")
    if not store.entity_exists(spec.context_document_urn):
        errors.append(f"context document is missing: {spec.context_document_urn}")
    document_info = store.get_aspect(spec.context_document_urn, "documentInfo")
    _require_fields(
        document_info,
        {
            "title": spec.context_document_title,
            "source": {"sourceType": "NATIVE"},
            "status": {"state": "PUBLISHED"},
            "contents": {"text": spec.context_document_content},
            "parentDocument": {"document": spec.shared_document_urn},
        },
        "DataRescue context document",
        errors,
    )
    _require_list_entry(
        document_info,
        "relatedAssets",
        {"asset": spec.source_urn},
        "context-document dataset relationship",
        errors,
    )
    _require_fields(
        store.get_aspect(spec.context_document_urn, "documentSettings"),
        {"showInGlobalContext": True},
        "context-document global visibility",
        errors,
    )
    _require_list_entry(
        store.get_aspect(spec.context_document_urn, "subTypes"),
        "typeNames",
        "Context",
        "context-document subtype",
        errors,
    )
    _require_list_entry(
        store.get_aspect(spec.context_document_urn, "ownership"),
        "owners",
        {"owner": spec.owner_urn, "type": "DATAOWNER"},
        "context-document owner",
        errors,
    )
    _require_list_entry(
        store.get_aspect(spec.context_document_urn, "glossaryTerms"),
        "terms",
        {"urn": spec.term_urn, "actor": spec.owner_urn},
        "context-document glossary term",
        errors,
    )
    try:
        related_documents = store.get_related_documents(spec.source_urn)
    except (RuntimeError, ValueError) as error:
        errors.append(f"dataset related-document lookup failed: {error}")
        return
    if not any(
        item.get("urn") == spec.context_document_urn
        and isinstance(item.get("info"), Mapping)
        and item["info"].get("title") == spec.context_document_title
        for item in related_documents
    ):
        errors.append("dataset does not expose the deterministic context document")


def _require_list_entry(
    actual: Mapping[str, Any] | None,
    list_key: str,
    expected: Mapping[str, Any] | str,
    label: str,
    errors: list[str],
) -> None:
    values = actual.get(list_key, []) if actual else []
    if isinstance(expected, str):
        found = expected in values
    else:
        found = any(isinstance(item, Mapping) and _contains(item, expected) for item in values)
    if not found:
        errors.append(f"{label} is missing or does not match the exact URN")


def _contains(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, Mapping):
            if not isinstance(actual_value, Mapping) or not _contains(
                actual_value, expected_value
            ):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _merge_required(
    current: Mapping[str, Any], required: Mapping[str, Any]
) -> dict[str, Any]:
    """Overlay required fields while retaining unknown metadata at every map level."""

    updated = copy.deepcopy(dict(current))
    for key, required_value in required.items():
        current_value = updated.get(key)
        if isinstance(required_value, Mapping) and isinstance(current_value, Mapping):
            updated[key] = _merge_required(current_value, required_value)
        else:
            updated[key] = copy.deepcopy(required_value)
    return updated


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "verify"))
    parser.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL"))
    parser.add_argument(
        "--source-urn", default=os.getenv("DATARESCUE_SOURCE_URN", SOURCE_ASSET_URN)
    )
    parser.add_argument(
        "--dbt-source-urn",
        default=os.getenv("DATARESCUE_DBT_SOURCE_URN", DBT_SOURCE_ASSET_URN),
    )
    parser.add_argument(
        "--staging-urn", default=os.getenv("DATARESCUE_STAGING_URN", STAGING_ASSET_URN)
    )
    parser.add_argument(
        "--staging-target-urn",
        default=os.getenv("DATARESCUE_STAGING_TARGET_URN", STAGING_TARGET_ASSET_URN),
    )
    parser.add_argument("--mart-urn", default=os.getenv("DATARESCUE_MART_URN", MART_ASSET_URN))
    return parser


StoreFactory = Callable[..., AspectStore]


def main(
    argv: Sequence[str] | None = None,
    *,
    store_factory: StoreFactory = SdkAspectStore,
) -> int:
    args = _build_parser().parse_args(argv)
    if not args.gms_url:
        print("DATAHUB_GMS_URL (or --gms-url) is required", file=sys.stderr)
        return 2
    spec = SemanticContextSpec(
        source_urn=args.source_urn,
        dbt_source_urn=args.dbt_source_urn,
        staging_urn=args.staging_urn,
        staging_target_urn=args.staging_target_urn,
        mart_urn=args.mart_urn,
    )
    try:
        token = os.getenv("DATAHUB_TOKEN") or os.getenv("DATARESCUE_DATAHUB_TOKEN") or None
        store = store_factory(gms_url=args.gms_url.rstrip("/"), token=token)
        if args.mode == "seed":
            changed = seed_context(store, spec)
            report = verify_context(store, spec, test_connection=False)
            report["changed_aspects"] = changed
        else:
            report = verify_context(store, spec)
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
