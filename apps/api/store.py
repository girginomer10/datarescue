from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from apps.api.models import (
    CandidateAssessment,
    CandidateProposal,
    CaseEvent,
    CaseSnapshot,
    CaseState,
    ContextBundle,
    EventType,
    IntegrationResult,
    PatchArtifact,
    PullRequestArtifact,
    SchemaChangeEvent,
    utc_now,
)
from apps.api.state_machine import validate_transition


class CaseNotFoundError(LookupError):
    pass


class DuplicateEventError(RuntimeError):
    def __init__(self, case_id: str) -> None:
        super().__init__(f"Schema event already belongs to case {case_id}")
        self.case_id = case_id


class EventStore:
    """Append-only SQLite event store.

    Case projections are derived at read time. Even demo reset is an event; no case
    rows are updated or deleted. A reset sequence forms a new deduplication scope.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state TEXT,
                    payload_json TEXT NOT NULL,
                    dedup_key TEXT,
                    reset_scope INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_events_dedup_scope
                ON events(dedup_key, reset_scope)
                WHERE dedup_key IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_events_id_scope
                ON events(event_id, reset_scope)
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_case_sequence "
                "ON events(case_id, sequence)"
            )

    @staticmethod
    def _current_scope(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS scope FROM events "
            "WHERE event_type = ?",
            (EventType.SYSTEM_RESET.value,),
        ).fetchone()
        return int(row["scope"])

    @staticmethod
    def _current_state(
        connection: sqlite3.Connection, case_id: str, scope: int
    ) -> CaseState | None:
        row = connection.execute(
            "SELECT state FROM events WHERE case_id = ? AND sequence > ? "
            "AND state IS NOT NULL ORDER BY sequence DESC LIMIT 1",
            (case_id, scope),
        ).fetchone()
        return CaseState(row["state"]) if row else None

    def append(
        self,
        *,
        case_id: str,
        event_type: EventType,
        state: CaseState,
        payload: dict[str, Any],
        dedup_key: str | None = None,
        event_id: str | None = None,
    ) -> CaseEvent:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        created_at = utc_now()
        event_id = event_id or str(uuid.uuid4())
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scope = self._current_scope(connection)
            current = self._current_state(connection, case_id, scope)
            validate_transition(current, state)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        event_id, case_id, event_type, state, payload_json,
                        dedup_key, reset_scope, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        case_id,
                        event_type.value,
                        state.value,
                        serialized,
                        dedup_key,
                        scope,
                        created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                # Either unique index — (dedup_key, reset_scope) or
                # (event_id, reset_scope) — can raise. Both mean an idempotent
                # duplicate of an existing case, never a crash: resolve to that
                # case instead of leaking a raw IntegrityError as a 500.
                if dedup_key is not None:
                    existing = connection.execute(
                        "SELECT case_id FROM events WHERE dedup_key = ? AND reset_scope = ?",
                        (dedup_key, scope),
                    ).fetchone()
                    if existing:
                        raise DuplicateEventError(str(existing["case_id"])) from error
                existing_by_id = connection.execute(
                    "SELECT case_id FROM events WHERE event_id = ? AND reset_scope = ?",
                    (event_id, scope),
                ).fetchone()
                if existing_by_id:
                    raise DuplicateEventError(str(existing_by_id["case_id"])) from error
                raise
            connection.execute("COMMIT")
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an event sequence")
            return CaseEvent(
                sequence=int(cursor.lastrowid),
                event_id=event_id,
                case_id=case_id,
                event_type=event_type,
                state=state,
                payload=payload,
                created_at=created_at,
            )

    def reset(self, reason: str = "Demo reset requested") -> int:
        created_at = utc_now()
        event_id = str(uuid.uuid4())
        payload = json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scope = self._current_scope(connection)
            cursor = connection.execute(
                """
                INSERT INTO events (
                    event_id, case_id, event_type, state, payload_json,
                    dedup_key, reset_scope, created_at
                ) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    event_id,
                    "__system__",
                    EventType.SYSTEM_RESET.value,
                    payload,
                    scope,
                    created_at.isoformat(),
                ),
            )
            connection.execute("COMMIT")
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a reset sequence")
            return int(cursor.lastrowid)

    def find_case_for_dedup(self, dedup_key: str) -> str | None:
        with self._connect() as connection:
            scope = self._current_scope(connection)
            row = connection.execute(
                "SELECT case_id FROM events WHERE dedup_key = ? AND reset_scope = ?",
                (dedup_key, scope),
            ).fetchone()
        return str(row["case_id"]) if row else None

    def events_for_case(self, case_id: str, after_sequence: int = 0) -> list[CaseEvent]:
        with self._connect() as connection:
            scope = self._current_scope(connection)
            rows = connection.execute(
                """
                SELECT sequence, event_id, case_id, event_type, state,
                       payload_json, created_at
                FROM events
                WHERE case_id = ? AND sequence > ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (case_id, scope, after_sequence),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def all_active_events(self) -> list[CaseEvent]:
        with self._connect() as connection:
            scope = self._current_scope(connection)
            rows = connection.execute(
                """
                SELECT sequence, event_id, case_id, event_type, state,
                       payload_json, created_at
                FROM events
                WHERE sequence > ? AND case_id != '__system__'
                ORDER BY sequence ASC
                """,
                (scope,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def raw_event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        return int(row["count"])

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> CaseEvent:
        return CaseEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            case_id=str(row["case_id"]),
            event_type=EventType(row["event_type"]),
            state=CaseState(row["state"]) if row["state"] else None,
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    def get_case(self, case_id: str) -> CaseSnapshot:
        events = self.events_for_case(case_id)
        if not events:
            raise CaseNotFoundError(case_id)
        return project_case(events)

    def list_cases(self) -> list[CaseSnapshot]:
        grouped: dict[str, list[CaseEvent]] = {}
        for event in self.all_active_events():
            grouped.setdefault(event.case_id, []).append(event)
        snapshots = [project_case(events) for events in grouped.values()]
        return sorted(snapshots, key=lambda case: case.updated_at, reverse=True)

    def contained_asset(self, asset_urn: str) -> CaseSnapshot | None:
        for case in self.list_cases():
            if case.asset_urn == asset_urn and case.state is CaseState.CONTAINED:
                return case
        return None


def _replace_assessment(
    assessments: list[CandidateAssessment], selected: CandidateAssessment
) -> list[CandidateAssessment]:
    return [selected if assessment.id == selected.id else assessment for assessment in assessments]


def project_case(events: Iterable[CaseEvent]) -> CaseSnapshot:
    ordered = list(events)
    if not ordered or ordered[0].event_type is not EventType.SCHEMA_CHANGE_DETECTED:
        raise ValueError("Case projection must begin with SCHEMA_CHANGE_DETECTED")
    first = ordered[0]
    schema_change = SchemaChangeEvent.model_validate(first.payload["schema_change"])
    snapshot = CaseSnapshot(
        id=first.case_id,
        asset_urn=schema_change.entity_urn,
        state=CaseState.DETECTED,
        created_at=first.created_at,
        updated_at=first.created_at,
        schema_change=schema_change,
        incident_urn=str(first.payload["incident_urn"]),
        events=[],
    )
    for event in ordered:
        if event.state is not None:
            snapshot.state = event.state
        snapshot.updated_at = event.created_at
        snapshot.events.append(event)
        payload = event.payload
        if event.event_type is EventType.INCIDENT_RAISED:
            snapshot.incident_integration = IntegrationResult.model_validate(payload["integration"])
            if snapshot.incident_integration.resource_id:
                snapshot.incident_urn = snapshot.incident_integration.resource_id
        elif event.event_type is EventType.CONTEXT_GATHERED:
            snapshot.context = ContextBundle.model_validate(payload["context"])
        elif event.event_type is EventType.CANDIDATES_READY:
            snapshot.candidate_generation = IntegrationResult.model_validate(
                payload["generation_integration"]
            )
            snapshot.candidate_proposals = [
                CandidateProposal.model_validate(item) for item in payload["candidates"]
            ]
        elif event.event_type is EventType.CANDIDATE_ASSESSED:
            assessment = CandidateAssessment.model_validate(payload["assessment"])
            snapshot.candidates.append(assessment)
        elif event.event_type is EventType.PATCH_READY:
            selected = CandidateAssessment.model_validate(payload["selected_candidate"])
            snapshot.selected_candidate = selected
            snapshot.candidates = _replace_assessment(snapshot.candidates, selected)
            snapshot.patch = PatchArtifact.model_validate(payload["patch"])
        elif event.event_type is EventType.EVIDENCE_WRITTEN:
            snapshot.evidence_writeback = IntegrationResult.model_validate(payload["integration"])
        elif event.event_type in {EventType.PR_ATTEMPTED, EventType.PR_OPENED}:
            snapshot.pull_request = PullRequestArtifact.model_validate(payload["pull_request"])
        elif event.event_type is EventType.DEPLOYMENT_RECORDED:
            snapshot.deployment_commit = str(payload["merged_commit_sha"])
        elif event.event_type is EventType.INCIDENT_RESOLVED:
            snapshot.incident_status = "RESOLVED"
        elif event.event_type is EventType.CONTAINED:
            snapshot.incident_status = "ACTIVE"
            snapshot.containment_reasons = [str(reason) for reason in payload["reasons"]]
            if payload.get("evidence_writeback"):
                snapshot.evidence_writeback = IntegrationResult.model_validate(
                    payload["evidence_writeback"]
                )
    return snapshot
