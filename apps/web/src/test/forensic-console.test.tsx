import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { CandidateMatrix } from "../components/case-sections";
import { DataSourceBanner, StageRail } from "../components/common";
import { containedReplayCase, replayPolicy, validatedReplayCase } from "../data/replay";
import { fetchCase, fetchCases, fetchPolicy } from "../lib/api";
import { CasePage, startCaseRefreshPolling } from "../pages/CasePage";
import { PolicyPage } from "../pages/PolicyPage";

function liveCasePayload(id: string) {
  return {
    id,
    asset_urn: "",
    state: "PATCH_READY",
    updated_at: "2026-07-22T10:00:00Z",
    incident_status: "ACTIVE",
    context: {
      owner: "Finance Data",
      integration: { status: "SUCCEEDED", message: "Live context gathered" },
    },
    candidates: [],
    events: [],
  };
}

function renderCasePage(client: QueryClient, caseId: string) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[`/cases/${caseId}`]}
        future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
      >
        <Routes>
          <Route path="/cases/:caseId" element={<CasePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Forensic Console evidence behavior", () => {
  it("keeps technical execution separate from the final policy decision", () => {
    render(<CandidateMatrix candidates={validatedReplayCase.candidates} />);

    const grossRow = screen.getByRole("row", { name: /gross_amount AS revenue/i });
    expect(within(grossRow).getByText("Semantic conflict")).toBeInTheDocument();
    expect(within(grossRow).getByText("+3.40%")).toBeInTheDocument();
    expect(within(grossRow).getByText("8/8 passed")).toBeInTheDocument();
    expect(within(grossRow).getByText("REJECTED")).toBeInTheDocument();

    const netRow = screen.getByRole("row", { name: /net_amount AS revenue/i });
    expect(within(netRow).getByText("Semantic match")).toBeInTheDocument();
    expect(within(netRow).getByText("0.00%")).toBeInTheDocument();
    expect(within(netRow).getByText("100%")).toBeInTheDocument();
    expect(within(netRow).getByText("SELECTED")).toBeInTheDocument();
  });

  it("gives a labeled section region an accessible name from its heading", () => {
    render(<CandidateMatrix candidates={validatedReplayCase.candidates} />);
    // The section uses aria-labelledby; the heading must render the matching id
    // or the region has no accessible name for assistive technology.
    expect(
      screen.getByRole("region", { name: /Candidate decision matrix/i }),
    ).toBeInTheDocument();
  });

  it("keeps the fail-closed replay aligned with the canonical settlement fixture", () => {
    render(<CandidateMatrix candidates={containedReplayCase.candidates} />);
    const row = screen.getByRole("row", { name: /settlement_amount AS revenue/i });
    expect(within(row).getByText("Evidence unknown")).toBeInTheDocument();
    expect(within(row).getByText("-1.50%")).toBeInTheDocument();
    expect(within(row).getByText("8/8 passed")).toBeInTheDocument();
    expect(within(row).getByText("REJECTED")).toBeInTheDocument();
  });

  it("labels replay evidence and explicitly refuses to imply live actions", () => {
    render(
      <DataSourceBanner
        envelope={{
          data: validatedReplayCase,
          mode: "replay",
          transport: "bundled-replay",
          reason: "connection refused",
        }}
      />,
    );

    expect(screen.getByText("RECORDED_REPLAY EVIDENCE")).toBeInTheDocument();
    expect(screen.getByText(/No recorded result is presented as a live integration run/i)).toBeInTheDocument();
  });

  it("labels API-returned replay provenance without claiming the API is unavailable", () => {
    render(
      <DataSourceBanner
        envelope={{
          data: validatedReplayCase,
          mode: "replay",
          transport: "api",
          reason: "Backend provenance: RECORDED_REPLAY (DataHub context)",
        }}
      />,
    );

    expect(screen.getByText(/API connected, but recorded workflow evidence is in use/i)).toBeInTheDocument();
    expect(screen.queryByText(/Live API unavailable/i)).not.toBeInTheDocument();
  });

  it("marks cached case evidence stale when a successful API load cannot be refreshed", async () => {
    const caseId = "DR-LIVE-REFRESH";
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({
          ok: true,
          json: async () => liveCasePayload(caseId),
        } as Response)
        .mockRejectedValueOnce(new Error("connection reset")),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderCasePage(client, caseId);

    expect(await screen.findByText("LIVE API")).toBeInTheDocument();
    expect(screen.getByText("Current")).toBeInTheDocument();
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["case", caseId] });
    });

    expect(await screen.findByText("STALE API SNAPSHOT")).toBeInTheDocument();
    expect(screen.getByText(/last successful snapshot remains visible for reference and is not live/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Captured context evidence" })).toBeInTheDocument();
    expect(screen.getByText("Captured pass")).toBeInTheDocument();
    expect(screen.queryByText("Current")).not.toBeInTheDocument();
    expect(screen.queryByText("Live context gathered")).not.toBeInTheDocument();
    expect(screen.queryByText(/Live case data returned by DataRescue API/i)).not.toBeInTheDocument();
    expect(screen.queryByText("LIVE API")).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("reports a reset or removed case without presenting its cached snapshot as live", async () => {
    const caseId = "DR-LIVE-RESET";
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({
          ok: true,
          json: async () => liveCasePayload(caseId),
        } as Response)
        .mockResolvedValueOnce({ ok: false, status: 404, statusText: "Not Found" } as Response),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderCasePage(client, caseId);

    expect(await screen.findByText("LIVE API")).toBeInTheDocument();
    expect(screen.getByText("Current")).toBeInTheDocument();
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["case", caseId] });
    });

    expect(await screen.findByText("CASE NO LONGER AVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/API reported that this case no longer exists/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Captured context evidence" })).toBeInTheDocument();
    expect(screen.getByText("Captured pass")).toBeInTheDocument();
    expect(screen.queryByText("Current")).not.toBeInTheDocument();
    expect(screen.queryByText("Live context gathered")).not.toBeInTheDocument();
    expect(screen.queryByText(/Live case data returned by DataRescue API/i)).not.toBeInTheDocument();
    expect(screen.queryByText("LIVE API")).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("falls back to the recorded queue only when the API is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const result = await fetchCases();

    expect(result.mode).toBe("replay");
    expect(result.transport).toBe("bundled-replay");
    expect(result.reason).toContain("offline");
    expect(result.data.map((item) => item.id)).toEqual(["DR-024", "DR-025"]);
    vi.unstubAllGlobals();
  });

  it("bounds an unavailable queue request before using replay evidence", async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal(
        "fetch",
        vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener(
              "abort",
              () => reject(new DOMException("The operation was aborted", "AbortError")),
              { once: true },
            );
          }),
        ),
      );

      const resultPromise = fetchCases();
      await vi.advanceTimersByTimeAsync(3_500);
      const result = await resultPromise;

      expect(result.transport).toBe("bundled-replay");
      expect(result.reason).toBe("API did not respond within 3500 ms");
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it("preserves an empty successful API queue instead of substituting replay cases", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => [] } as Response),
    );

    const result = await fetchCases();
    expect(result).toMatchObject({ data: [], mode: "live", transport: "api" });
    vi.unstubAllGlobals();
  });

  it("derives RECORDED_REPLAY mode from backend context and candidate provenance", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          id: "DR-REPLAY",
          asset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)",
          state: "PATCH_READY",
          context: {
            source: "RECORDED_REPLAY",
            owner: "Finance Data",
            integration: {
              status: "NOT_CONFIGURED",
              message: "MCP not configured",
              details: { fallback: "RECORDED_REPLAY" },
            },
          },
          candidate_generation: { status: "RECORDED_REPLAY" },
          candidates: [
            {
              id: "candidate-net",
              source_field: "net_amount",
              build: { passed: true, passed_checks: 8, total_checks: 8, command: "dbt build (recorded replay)" },
              reconciliation: { total_variance_pct: 0, primary_key_overlap_pct: 100 },
              evidence_refs: ["artifact://replay/DR-REPLAY/net_amount"],
              outcome: "SELECTED",
            },
          ],
        }),
      } as Response),
    );

    const result = await fetchCase("DR-REPLAY");
    expect(result.mode).toBe("replay");
    expect(result.transport).toBe("api");
    expect(result.reason).toContain("RECORDED_REPLAY");
    vi.unstubAllGlobals();
  });

  it("normalizes nested backend evidence without flattening away proof", async () => {
    const candidate = {
      id: "candidate-net",
      source_field: "net_amount",
      target_alias: "revenue",
      rationale: "Matches recognized net revenue",
      semantic_verdict: "MATCH",
      evidence_refs: ["urn:li:glossaryTerm:NetRevenue"],
      reconciliation: {
        total_variance_pct: 0,
        row_count_variance_pct: 0,
        primary_key_overlap_pct: 100,
        null_rate_delta_percentage_points: 0,
      },
      build: { passed: true, passed_checks: 8, total_checks: 8 },
      policy_checks: [
        { name: "total_variance", passed: true, observed: "0.00%", requirement: "≤ 0.50%" },
      ],
      outcome: "SELECTED",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          id: "DR-LIVE",
          asset_urn:
            "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)",
          state: "PATCH_READY",
          updated_at: "2026-07-22T10:00:00Z",
          incident_status: "ACTIVE",
          schema_change: {
            before_fields: [{ name: "amount" }],
            after_fields: [{ name: "gross_amount" }, { name: "net_amount" }],
          },
          context: {
            owner: "Finance Data",
            glossary_definition: "Recognized revenue is the net settled amount.",
            lineage_urns: ["urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.fct_revenue,PROD)"],
            context_documents: ["urn:li:glossaryTerm:NetRevenue"],
            lineage_current: true,
            integration: { status: "NOT_CONFIGURED", message: "Replay context" },
          },
          candidates: [candidate],
          selected_candidate: candidate,
          pull_request: {
            branch: "datarescue/dr-live",
            integration: { status: "NOT_RUN", message: "GitHub write disabled" },
          },
          events: [],
        }),
      } as Response),
    );

    const result = await fetchCase("DR-LIVE");
    expect(result.mode).toBe("live");
    expect(result.transport).toBe("api");
    expect(result.data.selectedCandidate).toBe("net_amount AS revenue");
    expect(result.data.candidates[0]).toMatchObject({
      totalVariance: 0,
      pkOverlap: 100,
      dbtPassed: 8,
      dbtTotal: 8,
      buildStatus: "passed",
      decision: "selected",
    });
    expect(result.data.outcomeTitle).toBe("SAFE REPAIR VALIDATED");
    expect(result.data.outcomeDetail).toMatch(/GitHub write was not run/i);
    const lineageNodeIds = new Set(result.data.lineageNodes.map((node) => node.id));
    expect(
      result.data.lineageEdges.every(
        (edge) => lineageNodeIds.has(edge.source) && lineageNodeIds.has(edge.target),
      ),
    ).toBe(true);
    vi.unstubAllGlobals();
  });

  it("does not mix provided lineage nodes with synthesized fallback edges", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          ...liveCasePayload("DR-PROVIDED-LINEAGE"),
          asset_urn:
            "urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)",
          context: {
            owner: "Finance Data",
            lineage_urns: [
              "urn:li:dataset:(urn:li:dataPlatform:dbt,datarescue.analytics.fct_revenue,PROD)",
            ],
            lineage_current: true,
            integration: { status: "SUCCEEDED", message: "Live context gathered" },
          },
          lineage_nodes: [
            {
              id: "provided-source",
              label: "payments_raw",
              kind: "Changed dataset",
              status: "affected",
              owner: "Finance Data",
            },
          ],
        }),
      } as Response),
    );

    const result = await fetchCase("DR-PROVIDED-LINEAGE");
    expect(result.data.lineageNodes.map((node) => node.id)).toEqual(["provided-source"]);
    expect(result.data.lineageEdges).toEqual([]);
    vi.unstubAllGlobals();
  });

  it("renders a case-specific not-found state instead of showing DR-024", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, statusText: "Not Found" } as Response),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter
          initialEntries={["/cases/DR-UNKNOWN"]}
          future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
        >
          <Routes>
            <Route path="/cases/:caseId" element={<CasePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Case not found")).toBeInTheDocument();
    expect(screen.getByText("Case DR-UNKNOWN was not found.")).toBeInTheDocument();
    expect(screen.queryByText(/CASE DR-024/i)).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("does not substitute a known replay case for an unknown case when offline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(fetchCase("DR-UNKNOWN")).rejects.toThrow("Could not load case DR-UNKNOWN: offline");
    vi.unstubAllGlobals();
  });

  it("labels completed stages as complete when no timestamp was returned", () => {
    render(
      <StageRail
        stages={[
          { id: "detected", label: "Detected", status: "complete" },
          { id: "validation", label: "Validation", status: "current" },
        ]}
      />,
    );
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(screen.getByText("In progress")).toBeInTheDocument();
  });

  it("schedules and cancels case refresh polling", () => {
    vi.useFakeTimers();
    const refresh = vi.fn();
    const stop = startCaseRefreshPolling(refresh, 1_000);
    vi.advanceTimersByTime(2_100);
    expect(refresh).toHaveBeenCalledTimes(2);
    stop();
    vi.advanceTimersByTime(2_000);
    expect(refresh).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("renders the backend threshold object as live policy rules", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          semantic_evidence_required: true,
          max_total_variance_pct: 0.5,
          max_row_count_variance_pct: 0.1,
          min_primary_key_overlap_pct: 99.9,
          max_null_rate_delta_percentage_points: 0.5,
          dbt_build_required: true,
          lineage_must_be_current: true,
        }),
      } as Response),
    );

    const result = await fetchPolicy();
    expect(result.mode).toBe("live");
    expect(result.data.rules).toHaveLength(7);
    expect(result.data.rules.find((rule) => rule.key === "max_total_variance_pct")?.value).toBe(
      "≤ 0.50%",
    );
    vi.unstubAllGlobals();
  });

  it("gives every policy region the accessible name declared by aria-labelledby", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
    });
    client.setQueryData(["policy"], {
      data: replayPolicy,
      mode: "replay",
      transport: "hosted-replay",
    });

    render(
      <QueryClientProvider client={client}>
        <PolicyPage />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("region", { name: "Required evidence gates" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Hard stops" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Automation boundary" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "policy.yaml" })).toBeInTheDocument();
  });
});
