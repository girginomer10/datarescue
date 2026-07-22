from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from apps.api.models import SchemaChangeEvent, SchemaField

try:  # The live DataHub Actions runtime provides this optional dependency.
    from datahub_actions.action.action import (  # type: ignore[import-not-found]
        Action as _DataHubActionBase,
    )
except ImportError:  # pragma: no cover - exercised only outside that runtime

    class _DataHubActionBase:  # type: ignore[no-redef]
        pass


class MCLActionStatus(StrEnum):
    ENQUEUED = "ENQUEUED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class MCLActionResult(BaseModel):
    status: MCLActionStatus
    reason: str
    case_id: str | None = None
    deduplicated: bool | None = None


class EventSink(Protocol):
    def __call__(self, event: SchemaChangeEvent) -> Any: ...


class DataHubSchemaMCLWatcher:
    """Filter DataHub MCLs and enqueue only real dataset schema transitions."""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink

    def handle(self, payload: Mapping[str, Any]) -> MCLActionResult:
        if not isinstance(payload, Mapping):
            # main()/act() may pass a value that is valid JSON but not an object
            # (e.g. a bare number or list); fail cleanly instead of AttributeError.
            return MCLActionResult(
                status=MCLActionStatus.FAILED,
                reason="MCL payload is not a JSON object",
            )
        event_type = payload.get("event_type") or payload.get("eventType")
        if event_type and event_type != "MetadataChangeLogEvent_v1":
            return MCLActionResult(
                status=MCLActionStatus.SKIPPED,
                reason=f"Unsupported event type: {event_type}",
            )
        if payload.get("entityType") != "dataset":
            return MCLActionResult(
                status=MCLActionStatus.SKIPPED, reason="Only dataset events are in scope"
            )
        if payload.get("aspectName") != "schemaMetadata":
            return MCLActionResult(
                status=MCLActionStatus.SKIPPED,
                reason="Only schemaMetadata changes are in scope",
            )
        previous_raw = payload.get("previousAspectValue")
        if previous_raw is None:
            return MCLActionResult(
                status=MCLActionStatus.SKIPPED,
                reason="Initial ingestion has no previous schema and is not drift",
            )
        try:
            previous = _object(previous_raw)
            current = _object(payload.get("aspect") or payload.get("aspectValue"))
            entity_urn = str(payload["entityUrn"])
            event = SchemaChangeEvent(
                event_id=_mcl_event_id(payload),
                entity_urn=entity_urn,
                before_fields=_schema_fields(previous),
                after_fields=_schema_fields(current),
                observed_at=_observed_at(payload),
                source="DATAHUB_MCL",
            )
            if _same_schema(event.before_fields, event.after_fields):
                return MCLActionResult(
                    status=MCLActionStatus.SKIPPED,
                    reason="schemaMetadata event contains no field-level change",
                )
            response = self.sink(event)
        except PermissionError:
            # DataHub streams MCLs for every dataset; a change to an asset outside
            # the remediation allowlist is out of scope, not an error to retry.
            return MCLActionResult(
                status=MCLActionStatus.SKIPPED,
                reason="Asset is outside the remediation allowlist",
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return MCLActionResult(
                status=MCLActionStatus.FAILED,
                reason=f"Invalid schemaMetadata MCL: {error}",
            )
        case = getattr(response, "case", None)
        case_id = getattr(case, "id", None)
        return MCLActionResult(
            status=MCLActionStatus.ENQUEUED,
            reason="Schema transition accepted by the idempotent workflow",
            case_id=str(case_id) if case_id is not None else None,
            deduplicated=getattr(response, "deduplicated", None),
        )


class HTTPEventSink:
    def __init__(
        self,
        api_url: str,
        *,
        timeout_seconds: float = 240,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = f"{api_url.rstrip('/')}/api/v1/events/schema-change"
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def __call__(self, event: SchemaChangeEvent) -> Any:
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(self.url, json=event.model_dump(mode="json"))
            response.raise_for_status()
            body = response.json()
        return _HTTPWorkflowResponse(body)


class _HTTPWorkflowResponse:
    def __init__(self, body: Mapping[str, Any]) -> None:
        case = body.get("case", {})
        self.case = type("CaseRef", (), {"id": case.get("id")})()
        self.deduplicated = body.get("deduplicated")


class DataRescueSchemaAction(_DataHubActionBase):  # type: ignore[misc]
    """Duck-typed DataHub Actions custom action entrypoint."""

    def __init__(self, api_url: str, *, request_timeout_seconds: float = 240) -> None:
        self.watcher = DataHubSchemaMCLWatcher(
            HTTPEventSink(api_url, timeout_seconds=request_timeout_seconds)
        )

    @classmethod
    def create(
        cls, config_dict: Mapping[str, Any], ctx: object | None = None
    ) -> DataRescueSchemaAction:
        del ctx
        api_url = config_dict.get("api_url")
        if not isinstance(api_url, str) or not api_url:
            raise ValueError("DataRescueSchemaAction requires a non-empty api_url")
        timeout = config_dict.get("request_timeout_seconds", 240)
        if not isinstance(timeout, int | float) or not 1 <= float(timeout) < 300:
            raise ValueError(
                "request_timeout_seconds must be numeric, >= 1 and below the Kafka poll interval"
            )
        return cls(api_url, request_timeout_seconds=float(timeout))

    def act(self, event: object) -> bool:
        """Process one DataHub Actions envelope.

        DataHub Actions supplies an ``EventEnvelope`` whose ``event`` member is
        usually a generated Pegasus ``MetadataChangeLogEvent`` object, not a
        plain mapping.  Normalize only its serialized data representation and
        raise on malformed events so the framework does not acknowledge a
        failed delivery.
        """

        payload = _event_payload(getattr(event, "event", event))
        result = self.watcher.handle(payload)
        if result.status is MCLActionStatus.FAILED:
            raise ValueError(result.reason)
        return True

    def close(self) -> None:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datarescue-mcl")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    watcher = DataHubSchemaMCLWatcher(HTTPEventSink(args.api_url))
    failed = False
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            result = watcher.handle(payload)
        except json.JSONDecodeError as error:
            result = MCLActionResult(
                status=MCLActionStatus.FAILED, reason=f"Invalid JSON: {error}"
            )
        print(result.model_dump_json())
        failed = failed or result.status is MCLActionStatus.FAILED
    return 1 if failed else 0


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and "value" in value:
        content_type = value.get("contentType")
        if content_type not in {None, "application/json"}:
            raise ValueError(f"Unsupported aspect content type: {content_type}")
        value = value["value"]
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Aspect value is not an object")
    return value


def _event_payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value

    for method_name in ("to_obj", "as_json"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        converted = method()
        if isinstance(converted, Mapping):
            return converted
        if isinstance(converted, bytes):
            converted = converted.decode("utf-8")
        if isinstance(converted, str):
            parsed = json.loads(converted)
            if isinstance(parsed, Mapping):
                return parsed
    raise ValueError("Action event cannot be normalized to a mapping")


def _schema_fields(aspect: Mapping[str, Any]) -> list[SchemaField]:
    raw_fields = aspect.get("fields")
    if not isinstance(raw_fields, list):
        raise ValueError("schemaMetadata has no fields array")
    fields: list[SchemaField] = []
    for raw in raw_fields:
        if not isinstance(raw, Mapping):
            raise ValueError("schemaMetadata field is not an object")
        name = raw.get("fieldPath") or raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("schemaMetadata field has no fieldPath")
        native = raw.get("nativeDataType") or _nested_type(raw.get("type")) or "unknown"
        fields.append(
            SchemaField(
                name=name,
                data_type=str(native),
                nullable=bool(raw.get("nullable", True)),
            )
        )
    return fields


def _nested_type(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        nested = value.get("type")
        if isinstance(nested, str):
            return nested
    return None


def _same_schema(before: list[SchemaField], after: list[SchemaField]) -> bool:
    def normalize(field: SchemaField) -> tuple[str, str, bool]:
        return (field.name, field.data_type, field.nullable)

    return sorted(map(normalize, before)) == sorted(map(normalize, after))


def _mcl_event_id(payload: Mapping[str, Any]) -> str:
    supplied = payload.get("eventId") or payload.get("id")
    if isinstance(supplied, str) and supplied:
        return supplied
    stable = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"mcl-{hashlib.sha256(stable.encode()).hexdigest()}"


def _from_millis(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # An out-of-range epoch (e.g. 10**20 ms) must not crash the watcher.
        return None


def _observed_at(payload: Mapping[str, Any]) -> datetime:
    created = payload.get("created")
    if isinstance(created, Mapping):
        parsed = _from_millis(created.get("time"))
        if parsed is not None:
            return parsed
    system = payload.get("systemMetadata")
    if isinstance(system, Mapping):
        parsed = _from_millis(system.get("lastObserved"))
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
