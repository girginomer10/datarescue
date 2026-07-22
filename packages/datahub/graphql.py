from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from apps.api.models import IntegrationResult, IntegrationStatus


class DataHubGraphQLError(RuntimeError):
    pass


class DataHubGraphQLAdapter:
    """Small incident lifecycle client that treats GraphQL errors as failures.

    DataHub may return HTTP 200 with a top-level ``errors`` field, so callers must
    never infer success from the HTTP status alone.
    """

    def __init__(
        self,
        *,
        gms_url: str | None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        readback_attempts: int = 5,
        readback_delay_seconds: float = 0.2,
    ) -> None:
        self.gms_url = gms_url.rstrip("/") if gms_url else None
        self.token = token
        self.transport = transport
        if readback_attempts < 1:
            raise ValueError("Incident read-back attempts must be at least one")
        if readback_delay_seconds < 0:
            raise ValueError("Incident read-back delay cannot be negative")
        self.readback_attempts = readback_attempts
        self.readback_delay_seconds = readback_delay_seconds

    def raise_incident(
        self, *, asset_urn: str, case_id: str, description: str
    ) -> IntegrationResult:
        if not self.gms_url:
            return self._not_configured("raise_incident")
        mutation = """
        mutation RaiseIncident($input: RaiseIncidentInput!) {
          raiseIncident(input: $input)
        }
        """
        title = f"DataRescue schema drift {case_id}"
        variables = {
            "input": {
                "resourceUrn": asset_urn,
                "type": "DATA_SCHEMA",
                "title": title,
                "description": description,
            }
        }
        try:
            data = self._execute(mutation, variables)
            resource_id = _find_urn(data.get("raiseIncident"))
            if not resource_id or not resource_id.startswith("urn:li:incident:"):
                raise DataHubGraphQLError("raiseIncident returned no valid incident URN")
            self._read_incident_info(
                resource_id,
                expected_state="ACTIVE",
                expected_asset_urn=asset_urn,
                expected_title=title,
            )
        except (httpx.HTTPError, DataHubGraphQLError, ValueError) as error:
            return IntegrationResult(
                status=IntegrationStatus.FAILED,
                operation="raise_incident",
                message=f"DataHub incident creation failed: {error}",
                details={"asset_urn": asset_urn},
            )
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="raise_incident",
            message="DataHub incident raised and left active",
            resource_id=resource_id,
            evidence_refs=[resource_id],
            details={"readback_verified": True, "remote_state": "ACTIVE"},
        )

    def resolve_incident(self, *, incident_urn: str) -> IntegrationResult:
        if not self.gms_url:
            return self._not_configured("resolve_incident")
        mutation = """
        mutation ResolveIncident($urn: String!, $input: IncidentStatusInput!) {
          updateIncidentStatus(urn: $urn, input: $input)
        }
        """
        try:
            if not incident_urn.startswith("urn:li:incident:"):
                raise DataHubGraphQLError("Incident resolution requires an incident URN")
            data = self._execute(
                mutation,
                {
                    "urn": incident_urn,
                    "input": {
                        "state": "RESOLVED",
                        "message": "DataRescue post-deploy evidence gates passed.",
                    },
                },
            )
            if data.get("updateIncidentStatus") in {False, None}:
                raise DataHubGraphQLError("updateIncidentStatus did not confirm success")
            self._read_incident_info(incident_urn, expected_state="RESOLVED")
        except (httpx.HTTPError, DataHubGraphQLError, ValueError) as error:
            return IntegrationResult(
                status=IntegrationStatus.FAILED,
                operation="resolve_incident",
                message=f"DataHub incident resolution failed: {error}",
                resource_id=incident_urn,
            )
        return IntegrationResult(
            status=IntegrationStatus.SUCCEEDED,
            operation="resolve_incident",
            message="DataHub incident resolved after post-deploy verification",
            resource_id=incident_urn,
            evidence_refs=[incident_urn],
            details={"readback_verified": True, "remote_state": "RESOLVED"},
        )

    def read_incident_state(self, incident_urn: str) -> str:
        """Read the persisted incident state from DataHub's incidentInfo aspect."""

        info = self._read_incident_info(incident_urn)
        return _validate_incident_info(info)

    def _read_incident_info(
        self,
        incident_urn: str,
        *,
        expected_state: str | None = None,
        expected_asset_urn: str | None = None,
        expected_title: str | None = None,
    ) -> dict[str, Any]:
        if not self.gms_url:
            raise DataHubGraphQLError("DataHub GMS is not configured")
        if not incident_urn.startswith("urn:li:incident:"):
            raise DataHubGraphQLError("Incident read-back requires an incident URN")
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        endpoint = (
            f"{self.gms_url}/openapi/v3/entity/incident/"
            f"{quote(incident_urn, safe='')}/incidentInfo"
        )
        last_error: Exception | None = None
        for attempt in range(1, self.readback_attempts + 1):
            try:
                with httpx.Client(timeout=20, transport=self.transport) as client:
                    response = client.get(endpoint, headers=headers)
                    response.raise_for_status()
                    body = response.json()
                if not isinstance(body, dict):
                    raise DataHubGraphQLError(
                        "incidentInfo read-back returned no value object"
                    )
                raw_info = body.get("value")
                if not isinstance(raw_info, dict):
                    raise DataHubGraphQLError(
                        "incidentInfo read-back returned no value object"
                    )
                info: dict[str, Any] = raw_info
                _validate_incident_info(
                    info,
                    expected_state=expected_state,
                    expected_asset_urn=expected_asset_urn,
                    expected_title=expected_title,
                )
                return info
            except (httpx.HTTPError, DataHubGraphQLError, ValueError) as error:
                last_error = error
            if attempt < self.readback_attempts and self.readback_delay_seconds:
                time.sleep(self.readback_delay_seconds)
        raise DataHubGraphQLError(
            "incident mutation was not confirmed by DataHub incidentInfo read-back "
            f"after {self.readback_attempts} attempt(s): {last_error}"
        )

    def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.gms_url:
            raise DataHubGraphQLError("DataHub GMS is not configured")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(timeout=20, transport=self.transport) as client:
            response = client.post(
                f"{self.gms_url}/api/graphql",
                headers=headers,
                json={"query": query, "variables": variables},
            )
            response.raise_for_status()
            body = response.json()
        if body.get("errors"):
            messages = "; ".join(
                str(item.get("message", item)) if isinstance(item, Mapping) else str(item)
                for item in body["errors"]
            )
            raise DataHubGraphQLError(messages)
        data = body.get("data")
        if not isinstance(data, dict):
            raise DataHubGraphQLError("GraphQL response has no data object")
        return data

    @staticmethod
    def _not_configured(operation: str) -> IntegrationResult:
        return IntegrationResult(
            status=IntegrationStatus.NOT_CONFIGURED,
            operation=operation,
            message="DATARESCUE_DATAHUB_GMS_URL is not configured; no remote write was claimed",
        )


def _find_urn(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("urn:"):
        return value
    if isinstance(value, dict):
        for key in ("urn", "incidentUrn", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("urn:"):
                return candidate
        for nested in value.values():
            candidate = _find_urn(nested)
            if candidate:
                return candidate
    if isinstance(value, list):
        for nested in value:
            candidate = _find_urn(nested)
            if candidate:
                return candidate
    return None


def _validate_incident_info(
    info: dict[str, Any],
    *,
    expected_state: str | None = None,
    expected_asset_urn: str | None = None,
    expected_title: str | None = None,
) -> str:
    status = info.get("status")
    if not isinstance(status, dict):
        raise DataHubGraphQLError("incidentInfo has no persisted status state")
    raw_state = status.get("state")
    if not isinstance(raw_state, str):
        raise DataHubGraphQLError("incidentInfo has no persisted status state")
    state = raw_state
    if expected_state is not None and state != expected_state:
        raise DataHubGraphQLError(
            f"incidentInfo state {state!r} does not match {expected_state!r}"
        )
    if expected_title is not None and info.get("title") != expected_title:
        raise DataHubGraphQLError("incidentInfo title does not match the requested incident")
    if expected_asset_urn is not None:
        entities = info.get("entities")
        if not isinstance(entities, list) or expected_asset_urn not in entities:
            raise DataHubGraphQLError(
                "incidentInfo is not linked to the requested DataHub asset"
            )
    return state
