from __future__ import annotations

import json

import httpx

from apps.api.models import IntegrationStatus
from packages.datahub.graphql import DataHubGraphQLAdapter
from packages.datahub.mcp import DataHubMCPAdapter


def test_unconfigured_datahub_adapters_never_claim_success() -> None:
    graphql = DataHubGraphQLAdapter(gms_url=None)
    mcp = DataHubMCPAdapter(endpoint=None)

    assert (
        graphql.raise_incident(asset_urn="urn:test", case_id="DR-1", description="test").status
        is IntegrationStatus.NOT_CONFIGURED
    )
    assert mcp.fetch_context("urn:test").integration.status is IntegrationStatus.NOT_CONFIGURED
    assert (
        mcp.write_evidence_report(
            asset_urn="urn:test", case_id="DR-1", report={}, degraded=True
        ).status
        is IntegrationStatus.NOT_CONFIGURED
    )


def test_graphql_http_200_with_errors_is_a_failed_operation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "permission denied"}]})

    adapter = DataHubGraphQLAdapter(
        gms_url="https://datahub.test", transport=httpx.MockTransport(handler)
    )

    result = adapter.raise_incident(
        asset_urn="urn:li:dataset:test", case_id="DR-1", description="test"
    )

    assert result.status is IntegrationStatus.FAILED
    assert "permission denied" in result.message


def test_graphql_incident_lifecycle_uses_datahub_1_6_inputs() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "raiseIncident" in body["query"]:
            assert body["variables"]["input"]["type"] == "DATA_SCHEMA"
            return httpx.Response(
                200, json={"data": {"raiseIncident": "urn:li:incident:dr-test"}}
            )
        assert body["variables"] == {
            "urn": "urn:li:incident:dr-test",
            "input": {
                "state": "RESOLVED",
                "message": "DataRescue post-deploy evidence gates passed.",
            },
        }
        return httpx.Response(200, json={"data": {"updateIncidentStatus": True}})

    adapter = DataHubGraphQLAdapter(
        gms_url="https://datahub.test", transport=httpx.MockTransport(handler)
    )

    raised = adapter.raise_incident(
        asset_urn="urn:li:dataset:test", case_id="DR-TEST", description="test"
    )
    resolved = adapter.resolve_incident(incident_urn="urn:li:incident:dr-test")

    assert raised.status is IntegrationStatus.SUCCEEDED
    assert resolved.status is IntegrationStatus.SUCCEEDED
    assert len(requests) == 2
