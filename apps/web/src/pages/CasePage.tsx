import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, GitPullRequest, Shield, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  CandidateMatrix,
  ContainmentPanel,
  DecisionSummaryIcon,
  DraftPrAction,
  EvidencePanel,
  IntegrationStrip,
  LineagePanel,
  RecoveryTimeline,
  SqlDiff,
  ValidationLedger,
} from "../components/case-sections";
import { DataSourceBanner, ErrorPanel, LoadingPanel, StageRail, StatusBadge, formatDateTime, toneForState } from "../components/common";
import { fetchCase } from "../lib/api";

export function startCaseRefreshPolling(refresh: () => void, intervalMs = 3_000): () => void {
  const timer = window.setInterval(refresh, intervalMs);
  return () => window.clearInterval(timer);
}

export function CasePage() {
  const { caseId = "" } = useParams();
  const queryClient = useQueryClient();
  const [announcement, setAnnouncement] = useState("");
  const query = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => fetchCase(caseId),
    enabled: Boolean(caseId),
    retry: false,
  });

  useEffect(() => {
    if (!caseId || query.data?.transport !== "api") return undefined;
    return startCaseRefreshPolling(() => {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["case", caseId] }),
        queryClient.invalidateQueries({ queryKey: ["cases"] }),
      ]);
    });
  }, [caseId, query.data?.transport, queryClient]);

  if (query.isLoading) {
    return (
      <main className="page" id="main-content">
        <LoadingPanel label={`Loading ${caseId} evidence`} />
      </main>
    );
  }
  // Once a case has loaded, keep rendering it even if a later poll fails (e.g. a
  // transient 404 after a reset); only fall to the error panel on initial load.
  if (!caseId || !query.data) {
    const message =
      query.error instanceof Error
        ? query.error.message
        : caseId
          ? `DataRescue could not load ${caseId}.`
          : "No case identifier was supplied.";
    const notFound = message.toLowerCase().includes("not found");
    return (
      <main className="page" id="main-content">
        <ErrorPanel title={notFound ? "Case not found" : "Case unavailable"} detail={message} />
        <Link className="text-link error-return-link" to="/">Return to rescue queue</Link>
      </main>
    );
  }

  const rescueCase = query.data.data;
  const isContained = rescueCase.state === "CONTAINED" || rescueCase.state === "FAILED";
  const prActionAvailable = rescueCase.state === "PR_OPEN" || Boolean(rescueCase.prUrl);

  return (
    <main className="page page--case" id="main-content">
      <nav className="breadcrumbs" aria-label="Breadcrumb">
        <Link to="/">Rescue queue</Link>
        <ChevronRight aria-hidden="true" size={13} />
        <span aria-current="page">{rescueCase.id}</span>
      </nav>

      <DataSourceBanner envelope={query.data} />

      <header className="case-hero">
        <div className="case-hero__identity">
          <div className="case-hero__topline">
            <p className="eyebrow">{rescueCase.eyebrow}</p>
            <StatusBadge label={rescueCase.stateLabel} tone={toneForState(rescueCase.state)} />
          </div>
          <h1>{rescueCase.title}</h1>
          <div className="case-meta">
            <span><Shield aria-hidden="true" size={14} /> {rescueCase.severity}</span>
            <span><UserRound aria-hidden="true" size={14} /> Owner · {rescueCase.owner}</span>
            <span>Incident · {rescueCase.incidentStatus}</span>
            <time dateTime={rescueCase.updatedAt}>Updated {formatDateTime(rescueCase.updatedAt)}</time>
          </div>
          <code className="asset-urn" title={rescueCase.assetUrn}>{rescueCase.assetUrn}</code>
          {rescueCase.affectedPath.length ? (
            <div className="affected-path" aria-label="Affected asset path">
              {rescueCase.affectedPath.map((asset, index) => (
                <span key={asset}>
                  <code>{asset}</code>
                  {index < rescueCase.affectedPath.length - 1 ? <ChevronRight aria-hidden="true" size={13} /> : null}
                </span>
              ))}
            </div>
          ) : null}
        </div>

        <section className={`decision-summary decision-summary--${rescueCase.outcomeTone}`} aria-labelledby="outcome-heading" aria-live="polite">
          <div className="decision-summary__icon">
            <DecisionSummaryIcon tone={rescueCase.outcomeTone} />
          </div>
          <div className="decision-summary__copy">
            <p className="eyebrow">POLICY OUTCOME</p>
            <h2 id="outcome-heading">{rescueCase.outcomeTitle}</h2>
            <p>{rescueCase.outcomeDetail}</p>
          </div>
          {prActionAvailable ? (
            <div className="decision-summary__action">
              <DraftPrAction
                prUrl={rescueCase.prUrl}
                onUnavailable={() => setAnnouncement("Recorded PR evidence has no live GitHub URL. No action was performed.")}
              />
              <span><GitPullRequest aria-hidden="true" size={13} /> HUMAN MERGE REQUIRED · INCIDENT REMAINS ACTIVE</span>
            </div>
          ) : null}
        </section>
        <div className="sr-only" aria-live="polite">{announcement}</div>
      </header>

      <IntegrationStrip integrations={rescueCase.integrations} />
      <StageRail stages={rescueCase.stages} />
      <CandidateMatrix candidates={rescueCase.candidates} />

      <div className="case-grid case-grid--context">
        <LineagePanel nodes={rescueCase.lineageNodes} edges={rescueCase.lineageEdges} />
        <EvidencePanel evidence={rescueCase.evidence} recorded={query.data.mode === "replay"} />
      </div>

      <div className="case-grid case-grid--proof">
        <SqlDiff lines={rescueCase.diff} />
        <ValidationLedger entries={rescueCase.validationLedger} />
      </div>

      <div className="case-grid case-grid--resolution">
        <RecoveryTimeline entries={rescueCase.timeline} />
        <ContainmentPanel proof={rescueCase.containment} active={isContained} />
      </div>
    </main>
  );
}
