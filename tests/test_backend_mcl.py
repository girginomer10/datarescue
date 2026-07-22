from __future__ import annotations

import json

from apps.api.models import SchemaChangeEvent
from apps.api.workflow import DEFAULT_ASSET_URN
from packages.datahub.actions import (
    DataHubSchemaMCLWatcher,
    DataRescueSchemaAction,
    MCLActionStatus,
)


def _mcl(previous: object) -> dict[str, object]:
    return {
        "event_type": "MetadataChangeLogEvent_v1",
        "entityType": "dataset",
        "aspectName": "schemaMetadata",
        "entityUrn": DEFAULT_ASSET_URN,
        "previousAspectValue": (
            None
            if previous is None
            else {"contentType": "application/json", "value": json.dumps(previous)}
        ),
        "aspect": {
            "contentType": "application/json",
            "value": json.dumps(
                {
                    "fields": [
                        {
                            "fieldPath": "payment_id",
                            "nativeDataType": "bigint",
                            "nullable": False,
                        },
                        {
                            "fieldPath": "gross_amount",
                            "nativeDataType": "numeric",
                            "nullable": False,
                        },
                        {
                            "fieldPath": "net_amount",
                            "nativeDataType": "numeric",
                            "nullable": False,
                        },
                    ]
                }
            ),
        },
        "systemMetadata": {"lastObserved": 1785000000000},
    }


def test_mcl_watcher_skips_initial_ingestion() -> None:
    captured: list[SchemaChangeEvent] = []
    watcher = DataHubSchemaMCLWatcher(captured.append)

    result = watcher.handle(_mcl(None))

    assert result.status is MCLActionStatus.SKIPPED
    assert "Initial ingestion" in result.reason
    assert captured == []


def test_mcl_watcher_converts_schema_metadata_transition() -> None:
    captured: list[SchemaChangeEvent] = []
    watcher = DataHubSchemaMCLWatcher(captured.append)
    previous = {
        "fields": [
            {
                "fieldPath": "payment_id",
                "nativeDataType": "bigint",
                "nullable": False,
            },
            {
                "fieldPath": "amount",
                "nativeDataType": "numeric",
                "nullable": False,
            },
        ]
    }

    result = watcher.handle(_mcl(previous))

    assert result.status is MCLActionStatus.ENQUEUED
    assert len(captured) == 1
    event = captured[0]
    assert event.entity_urn == DEFAULT_ASSET_URN
    assert [field.name for field in event.before_fields] == ["payment_id", "amount"]
    assert [field.name for field in event.after_fields] == [
        "payment_id",
        "gross_amount",
        "net_amount",
    ]
    assert event.source == "DATAHUB_MCL"


def test_mcl_watcher_filters_unrelated_aspects() -> None:
    captured: list[SchemaChangeEvent] = []
    watcher = DataHubSchemaMCLWatcher(captured.append)
    payload = _mcl({"fields": []})
    payload["aspectName"] = "ownership"

    result = watcher.handle(payload)

    assert result.status is MCLActionStatus.SKIPPED
    assert captured == []


def test_datahub_action_normalizes_pegasus_event_objects() -> None:
    class _PegasusEvent:
        def to_obj(self) -> dict[str, object]:
            previous = {
                "fields": [
                    {"fieldPath": "payment_id", "nativeDataType": "bigint"},
                    {"fieldPath": "amount", "nativeDataType": "numeric"},
                ]
            }
            return _mcl(previous)

    class _Envelope:
        event = _PegasusEvent()

    action = DataRescueSchemaAction("https://api.test")
    captured: list[SchemaChangeEvent] = []
    action.watcher = DataHubSchemaMCLWatcher(captured.append)

    assert action.act(_Envelope()) is True
    assert len(captured) == 1
    assert captured[0].entity_urn == DEFAULT_ASSET_URN
