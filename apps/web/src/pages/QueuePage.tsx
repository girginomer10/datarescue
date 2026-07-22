import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  Play,
  RotateCcw,
  ShieldAlert,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { DataSourceBanner, ErrorPanel, LoadingPanel, StatusBadge, formatDateTime, toneForState } from "../components/common";
import { fetchCases, runDemoAction } from "../lib/api";
import type { CaseSummary } from "../types";

type QueueFilter = "all" | "review" | "contained" | "recovered";

function matchesFilter(item: CaseSummary, filter: QueueFilter): boolean {
  if (filter === "review") return item.state === "PR_OPEN";
  if (filter === "contained") return item.state === "CONTAINED" || item.state === "FAILED";
  if (filter === "recovered") return item.state === "RESOLVED";
  return true;
}

export function QueuePage() {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [announcement, setAnnouncement] = useState("");
  const query = useQuery({ queryKey: ["cases"], queryFn: fetchCases, retry: false });
  const action = useMutation({
    mutationFn: runDemoAction,
    onSuccess: async (_, kind) => {
      setAnnouncement(kind === "drift" ? "Live schema drift triggered." : "Live demo state reset.");
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
    },
    onError: (error) => setAnnouncement(`Live action failed: ${error instanceof Error ? error.message : "Unknown error"}`),
  });

  const cases = query.data?.data ?? [];
  const visibleCases = useMemo(() => cases.filter((item) => matchesFilter(item, filter)), [cases, filter]);
  const counts = useMemo(
    () => ({
      review: cases.filter((item) => item.state === "PR_OPEN").length,
      contained: cases.filter((item) => item.state === "CONTAINED" || item.state === "FAILED").length,
      recovered: cases.filter((item) => item.state === "RESOLVED").length,
    }),
    [cases],
  );
  const liveActionsAvailable = query.data?.transport === "api";

  return (
    <main className="page page--queue" id="main-content">
      <div className="page-heading page-heading--queue">
        <div>
          <p className="eyebrow">RUNTIME RECOVERY OPERATIONS</p>
          <h1>Rescue queue</h1>
          <p>Evidence-gated schema-drift cases. Prove the fix before it ships.</p>
        </div>
        <div className="demo-actions" aria-label="Live demo controls">
          <button
            className="secondary-button"
            disabled={!liveActionsAvailable || action.isPending}
            onClick={() => action.mutate("reset")}
            title={liveActionsAvailable ? "Reset live demo" : "Requires the live API"}
            type="button"
          >
            <RotateCcw aria-hidden="true" size={15} /> Reset
          </button>
          <button
            className="primary-outline-button"
            disabled={!liveActionsAvailable || action.isPending}
            onClick={() => action.mutate("drift")}
            title={liveActionsAvailable ? "Trigger live schema drift" : "Requires the live API"}
            type="button"
          >
            <Play aria-hidden="true" size={15} /> Trigger drift
          </button>
          {!liveActionsAvailable ? <span className="action-lock">LIVE API REQUIRED</span> : null}
        </div>
      </div>
      <div className="sr-only" aria-live="polite">{announcement}</div>

      {query.isLoading ? <LoadingPanel label="Loading rescue queue" /> : null}
      {query.isError ? <ErrorPanel title="Queue unavailable" detail="DataRescue could not load case data." /> : null}
      {query.data ? <DataSourceBanner envelope={query.data} /> : null}

      <section className="queue-stats" aria-label="Queue summary">
        <article>
          <span className="stat-icon stat-icon--warning"><Clock3 aria-hidden="true" size={18} /></span>
          <div><strong>{counts.review}</strong><span>Awaiting review</span></div>
          <small>Human decision required</small>
        </article>
        <article>
          <span className="stat-icon stat-icon--danger"><ShieldAlert aria-hidden="true" size={18} /></span>
          <div><strong>{counts.contained}</strong><span>Contained</span></div>
          <small>Downstream blocked</small>
        </article>
        <article>
          <span className="stat-icon stat-icon--success"><CheckCircle2 aria-hidden="true" size={18} /></span>
          <div><strong>{counts.recovered}</strong><span>Recovered</span></div>
          <small>Post-deploy proof passed</small>
        </article>
        <article>
          <span className="stat-icon stat-icon--context"><AlertTriangle aria-hidden="true" size={18} /></span>
          <div><strong>{cases.length}</strong><span>Total evidence cases</span></div>
          <small>Current queue snapshot</small>
        </article>
      </section>

      <section className="panel queue-panel" aria-labelledby="queue-table-title">
        <div className="queue-toolbar">
          <div>
            <p className="eyebrow">INCIDENT WORKLIST</p>
            <h2 id="queue-table-title">Schema drift cases</h2>
          </div>
          <div className="filter-tabs" role="tablist" aria-label="Filter rescue cases">
            {(
              [
                ["all", "All"],
                ["review", "Needs review"],
                ["contained", "Contained"],
                ["recovered", "Recovered"],
              ] as Array<[QueueFilter, string]>
            ).map(([value, label]) => (
              <button
                aria-selected={filter === value}
                key={value}
                onClick={() => setFilter(value)}
                role="tab"
                type="button"
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="table-scroll">
          <table className="queue-table">
            <caption className="sr-only">DataRescue cases and their current recovery state</caption>
            <thead>
              <tr>
                <th scope="col">Case</th>
                <th scope="col">Asset</th>
                <th scope="col">Impact / owner</th>
                <th scope="col">Decision</th>
                <th scope="col">Incident</th>
                <th scope="col">Updated</th>
                <th scope="col"><span className="sr-only">Open case</span></th>
              </tr>
            </thead>
            <tbody>
              {visibleCases.map((item) => (
                <tr key={item.id}>
                  <th scope="row">
                    <Link className="case-link" to={`/cases/${item.id}`}>
                      <span>{item.id}</span>
                      <strong>{item.title}</strong>
                    </Link>
                  </th>
                  <td><code title={item.assetUrn}>{item.assetUrn}</code></td>
                  <td><strong>{item.severity}</strong><small>{item.owner}</small></td>
                  <td>
                    <StatusBadge label={item.stateLabel} tone={toneForState(item.state)} />
                    {item.selectedCandidate ? <small className="selected-mapping">{item.selectedCandidate}</small> : null}
                  </td>
                  <td><span className={`incident-state incident-state--${item.incidentStatus.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}>{item.incidentStatus}</span></td>
                  <td><time dateTime={item.updatedAt}>{formatDateTime(item.updatedAt)}</time></td>
                  <td>
                    <Link aria-label={`Open ${item.id}`} className="row-action" to={`/cases/${item.id}`}>
                      <ArrowRight aria-hidden="true" size={16} />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!visibleCases.length && !query.isLoading ? (
            <div className="empty-section">
              {cases.length === 0 && query.data?.transport === "api"
                ? "No cases in the current API queue."
                : "No cases match this filter."}
            </div>
          ) : null}
        </div>
      </section>
    </main>
  );
}
