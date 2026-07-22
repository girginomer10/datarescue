from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
    ) -> None:
        self.gms_url = gms_url.rstrip("/") if gms_url else None
        self.token = token
        self.transport = transport

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
        variables = {
            "input": {
                "resourceUrn": asset_urn,
                "type": "DATA_SCHEMA",
                "title": f"DataRescue schema drift {case_id}",
                "description": description,
            }
        }
        try:
            data = self._execute(mutation, variables)
            resource_id = _find_urn(data.get("raiseIncident"))
            if not resource_id:
                raise DataHubGraphQLError("raiseIncident returned no incident URN")
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
