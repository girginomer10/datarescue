import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Ban,
  Check,
  CheckCircle2,
  Clipboard,
  Clock3,
  Code2,
  Copy,
  Database,
  ExternalLink,
  FileCheck2,
  GitBranch,
  Layers3,
  Link2,
  ListTree,
  ShieldAlert,
  Table2,
  X,
  XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import type {
  Candidate,
  ContainmentProof,
  DiffLine,
  EvidenceItem,
  IntegrationStatus,
  LineageEdgeData,
  LineageNodeData,
  TimelineEntry,
  ValidationEntry,
} from "../types";
import { SectionHeading, StatusBadge } from "./common";

const integrationIcons = {
  datahub: Layers3,
  postgres: Database,
  dbt: Activity,
  github: GitBranch,
};

function artifactHref(reference: string): string | undefined {
  if (/^https?:\/\//i.test(reference)) return reference;
  if (!reference.startsWith("artifacts/")) return undefined;
  return `${import.meta.env.BASE_URL}${reference}`;
}

function ArtifactReference({ reference, className }: { reference: string; className?: string }) {
  const href = artifactHref(reference);
  const content = (
    <code className={className} title={reference}>
      <Link2 aria-hidden="true" size={12} /> {reference}
    </code>
  );
  return href ? (
    <a className="artifact-link" href={href} target="_blank" rel="noreferrer">
      {content}
    </a>
  ) : (
    content
  );
}

export function IntegrationStrip({
  integrations,
  captured = false,
}: {
  integrations: IntegrationStatus[];
  captured?: boolean;
}) {
  if (!integrations.length) {
    return (
      <p className="empty-note">
        {captured
          ? "Integration health was not present in the captured API snapshot."
          : "Integration health was not returned by the live API."}
      </p>
    );
  }
  return (
    <ul
      className="case-integrations"
      aria-label={captured ? "Captured case integration status" : "Case integration status"}
    >
      {integrations.map((integration) => {
        const Icon = integrationIcons[integration.id];
        return (
          <li
            className={`case-integration case-integration--${captured ? "pending" : integration.state}`}
            key={integration.id}
          >
            <Icon aria-hidden="true" size={17} strokeWidth={1.8} />
            <span>
              <strong>{integration.label}</strong>
              <small>
                {captured
                  ? "Last reported integration state; refresh required."
                  : integration.detail}
              </small>
            </span>
            <span className="case-integration__state">
              {captured
                ? "CAPTURED"
                : integration.state === "ready"
                ? "READY"
                : integration.state === "pending"
                  ? "PENDING"
                  : integration.state === "degraded"
                    ? "DEGRADED"
                    : "OFFLINE"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function SemanticVerdict({ candidate }: { candidate: Candidate }) {
  if (candidate.semanticVerdict === "match") {
    return (
      <div className="matrix-verdict matrix-verdict--success">
        <CheckCircle2 aria-hidden="true" size={16} />
        <div>
          <strong>Semantic match</strong>
          <span>{candidate.semanticDetail}</span>
        </div>
      </div>
    );
  }
  if (candidate.semanticVerdict === "conflict") {
    return (
      <div className="matrix-verdict matrix-verdict--danger">
        <XCircle aria-hidden="true" size={16} />
        <div>
          <strong>Semantic conflict</strong>
          <span>{candidate.semanticDetail}</span>
        </div>
      </div>
    );
  }
  return (
    <div className="matrix-verdict matrix-verdict--warning">
      <AlertTriangle aria-hidden="true" size={16} />
      <div>
        <strong>Evidence unknown</strong>
        <span>{candidate.semanticDetail}</span>
      </div>
    </div>
  );
}

export function CandidateMatrix({ candidates }: { candidates: Candidate[] }) {
  return (
    <section className="panel panel--decision" aria-labelledby="candidate-matrix-title">
      <SectionHeading
        eyebrow="DETERMINISTIC DECISION"
        title="Candidate decision matrix"
        titleId="candidate-matrix-title"
        detail="A successful build is necessary, not sufficient. Every evidence gate must pass."
        action={<StatusBadge label="0.50% variance ceiling" tone="neutral" />}
      />
      {candidates.length ? (
        <div className="table-scroll">
          <table className="candidate-table">
            <caption className="sr-only">
              Candidate repairs compared across semantic evidence, reconciliation, dbt execution, and final policy decision
            </caption>
            <thead>
              <tr>
                <th scope="col">Candidate mapping</th>
                <th scope="col">Semantic evidence</th>
                <th scope="col">Total variance</th>
                <th scope="col">PK overlap</th>
                <th scope="col">dbt build</th>
                <th scope="col">Policy decision</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((candidate) => {
                const variancePass = Math.abs(candidate.totalVariance) <= 0.5;
                return (
                  <tr className={`candidate-row candidate-row--${candidate.decision}`} key={candidate.id}>
                    <th scope="row">
                      <code>{candidate.expression}</code>
                      <span className="candidate-id">{candidate.id.toUpperCase()}</span>
                    </th>
                    <td>
                      <SemanticVerdict candidate={candidate} />
                    </td>
                    <td>
                      <span className={`gate-number gate-number--${variancePass ? "pass" : "fail"}`}>
                        {candidate.totalVariance > 0 ? "+" : ""}
                        {candidate.totalVariance.toFixed(2)}%
                      </span>
                      <small>{variancePass ? "Within policy" : "Exceeds 0.50%"}</small>
                    </td>
                    <td>
                      <span className={`gate-number gate-number--${candidate.pkOverlap >= 99.9 ? "pass" : "fail"}`}>
                        {candidate.pkOverlap.toFixed(0)}%
                      </span>
                      <small>Minimum 99.90%</small>
                    </td>
                    <td>
                      <span className={`technical-status technical-status--${candidate.buildStatus}`}>
                        {candidate.buildStatus === "passed" ? <Check aria-hidden="true" size={14} /> : <X aria-hidden="true" size={14} />}
                        {candidate.buildStatus === "passed"
                          ? `${candidate.dbtPassed}/${candidate.dbtTotal} passed`
                          : candidate.buildStatus.replace("_", " ")}
                      </span>
                      <small>Technical execution</small>
                    </td>
                    <td className={`decision-cell decision-cell--${candidate.decision}`}>
                      <span>
                        {candidate.decision === "selected" ? (
                          <CheckCircle2 aria-hidden="true" size={17} />
                        ) : candidate.decision === "rejected" ? (
                          <Ban aria-hidden="true" size={17} />
                        ) : (
                          <Clock3 aria-hidden="true" size={17} />
                        )}
                        {candidate.decision.toUpperCase()}
                      </span>
                      <small>{candidate.reason}</small>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-section">No candidate assessments were returned by the API.</div>
      )}
    </section>
  );
}

export function LineagePanel({
  nodes,
  edges,
  captured = false,
}: {
  nodes: LineageNodeData[];
  edges: LineageEdgeData[];
  captured?: boolean;
}) {
  const [view, setView] = useState<"graph" | "table">("graph");
  const flowNodes = useMemo<Node[]>(
    () =>
      nodes.map((node, index) => ({
        id: node.id,
        position: { x: index * 238, y: index % 2 === 0 ? 55 : 88 },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        draggable: false,
        selectable: true,
        data: {
          label: (
            <div className={`lineage-node lineage-node--${node.status}`}>
              <span>{node.kind}</span>
              <strong>{node.label}</strong>
              <small>{node.owner}</small>
            </div>
          ),
        },
        className: `flow-node flow-node--${node.status}`,
        ariaLabel: `${node.label}, ${node.kind}, ${node.status}, owner ${node.owner}`,
      })),
    [nodes],
  );
  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge, index) => ({
        id: `edge-${index}-${edge.source}-${edge.target}`,
        source: edge.source,
        target: edge.target,
        label: edge.relationship,
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed, color: "#6f7b89", width: 15, height: 15 },
        style: { stroke: "#6f7b89", strokeWidth: 1.4 },
        labelStyle: { fill: "#9da7b3", fontSize: 10, fontFamily: "Inter, sans-serif" },
        labelBgStyle: { fill: "#111923", fillOpacity: 0.94 },
      })),
    [edges],
  );

  return (
    <section className="panel lineage-panel" aria-labelledby="lineage-title">
      <SectionHeading
        eyebrow={captured ? "CAPTURED DATAHUB CONTEXT" : "DATAHUB CONTEXT"}
        title={captured ? "Captured affected lineage" : "Affected lineage"}
        titleId="lineage-title"
        detail={
          captured
            ? "Last successful lineage snapshot; the current blast radius is unverified."
            : "Runtime blast radius captured at detection time."
        }
        action={
          <div className="segmented-control" role="tablist" aria-label="Lineage view">
            <button
              aria-controls="lineage-graph-panel"
              aria-selected={view === "graph"}
              id="lineage-graph-tab"
              onClick={() => setView("graph")}
              role="tab"
              type="button"
            >
              <ListTree aria-hidden="true" size={14} /> Graph
            </button>
            <button
              aria-controls="lineage-table-panel"
              aria-selected={view === "table"}
              id="lineage-table-tab"
              onClick={() => setView("table")}
              role="tab"
              type="button"
            >
              <Table2 aria-hidden="true" size={14} /> Table
            </button>
          </div>
        }
      />
      {/* Both tabpanels stay mounted so each tab's aria-controls always resolves;
          the heavy ReactFlow graph is only instantiated while its tab is active. */}
      <div
        id="lineage-graph-panel"
        role="tabpanel"
        aria-labelledby="lineage-graph-tab"
        className="lineage-canvas"
        hidden={view !== "graph"}
      >
        {view === "graph" ? (
          nodes.length ? (
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              fitView
              fitViewOptions={{ padding: 0.16 }}
              nodesConnectable={false}
              nodesDraggable={false}
              minZoom={0.7}
              maxZoom={1.4}
              proOptions={{ hideAttribution: true }}
            >
              <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#263140" />
              <Controls showInteractive={false} position="bottom-right" />
            </ReactFlow>
          ) : (
            <div className="empty-section">No lineage graph was returned by the API.</div>
          )
        ) : null}
      </div>
      <div
        id="lineage-table-panel"
        role="tabpanel"
        aria-labelledby="lineage-table-tab"
        className="table-scroll lineage-table-wrap"
        hidden={view !== "table"}
      >
        <table className="lineage-table">
            <caption className="sr-only">Accessible table equivalent of the affected lineage graph</caption>
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Asset</th>
                <th scope="col">Type</th>
                <th scope="col">Owner</th>
                <th scope="col">Impact</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node, index) => (
                <tr key={node.id}>
                  <td className="mono">{String(index + 1).padStart(2, "0")}</td>
                  <th scope="row" className="mono">{node.label}</th>
                  <td>{node.kind}</td>
                  <td>{node.owner}</td>
                  <td><StatusBadge label={node.status} tone={node.status === "healthy" ? "success" : node.status === "affected" ? "warning" : "danger"} /></td>
                </tr>
              ))}
            </tbody>
          </table>
      </div>
    </section>
  );
}

function verdictTone(verdict: EvidenceItem["verdict"]): "success" | "danger" | "warning" {
  return verdict === "pass" ? "success" : verdict === "fail" ? "danger" : "warning";
}

export type EvidenceProvenance = "live" | "recorded" | "stale";

export function EvidencePanel({
  evidence,
  provenance = "live",
}: {
  evidence: EvidenceItem[];
  provenance?: EvidenceProvenance;
}) {
  const recorded = provenance === "recorded";
  const stale = provenance === "stale";
  return (
    <section className="panel evidence-panel" aria-labelledby="evidence-title">
      <SectionHeading
        eyebrow={
          stale ? "CAPTURED API SNAPSHOT" : recorded ? "RECORDED CONTEXT" : "RETRIEVED, NOT ASSUMED"
        }
        title={
          stale
            ? "Captured context evidence"
            : recorded
              ? "Recorded context evidence"
              : "DataHub evidence"
        }
        titleId="evidence-title"
        detail={
          stale
            ? "Claims are retained from the last successful response; their current validity is unverified."
            : recorded
            ? "Replay claims retain their captured source and freshness metadata."
            : "Every semantic claim keeps its source and freshness."
        }
      />
      {evidence.length ? (
        <div className="evidence-list">
          {evidence.map((item) => (
            <article className={`evidence-card evidence-card--${item.verdict}`} key={item.id}>
              <div className="evidence-card__topline">
                <span>{item.sourceType}</span>
                <StatusBadge
                  label={
                    stale
                      ? `Captured ${item.verdict}`
                      : item.verdict === "pass"
                        ? recorded
                          ? "Recorded"
                          : "Current"
                        : item.verdict
                  }
                  tone={stale ? "warning" : verdictTone(item.verdict)}
                />
              </div>
              <p>{item.claim}</p>
              <code title={item.source}>{item.source}</code>
              <small>{stale ? `Captured snapshot · ${item.freshness}` : item.freshness}</small>
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-section">No DataHub evidence was returned. Policy cannot pass without it.</div>
      )}
    </section>
  );
}

export function SqlDiff({ lines }: { lines: DiffLine[] }) {
  const [announcement, setAnnouncement] = useState("");
  const copyDiff = async () => {
    try {
      await navigator.clipboard.writeText(lines.map((line) => line.content).join("\n"));
      setAnnouncement("SQL diff copied to clipboard.");
    } catch {
      setAnnouncement("Clipboard access is unavailable. Diff was not copied.");
    }
  };

  return (
    <section className="panel diff-panel" aria-labelledby="diff-title">
      <SectionHeading
        eyebrow="SELECTED PATCH"
        title="dbt SQL diff"
        titleId="diff-title"
        detail="Rendered by the allowlisted patch generator."
        action={
          <button className="icon-text-button" onClick={copyDiff} type="button" disabled={!lines.length}>
            <Copy aria-hidden="true" size={14} /> Copy diff
          </button>
        }
      />
      <div aria-live="polite" className="sr-only">{announcement}</div>
      {lines.length ? (
        <pre className="sql-diff" aria-label="Selected SQL patch">
          {lines.map((line, index) => (
            <span className={`diff-line diff-line--${line.kind}`} key={`${line.kind}-${line.number}-${index}`}>
              <span className="diff-line__number" aria-hidden="true">{line.number}</span>
              <code>{line.content}</code>
            </span>
          ))}
        </pre>
      ) : (
        <div className="empty-section">No patch has been selected.</div>
      )}
      <div className="diff-footer">
        <Code2 aria-hidden="true" size={14} />
        <span>models/staging/stg_payments.sql</span>
        <span>SELECT-only allowlist</span>
      </div>
    </section>
  );
}

export function ValidationLedger({ entries }: { entries: ValidationEntry[] }) {
  return (
    <section className="panel ledger-panel" aria-labelledby="ledger-title">
      <SectionHeading
        eyebrow="APPEND-ONLY PROOF"
        title="Validation ledger"
        titleId="ledger-title"
        detail="Measured against audit.payments_fct_last_good."
      />
      {entries.length ? (
        <div className="table-scroll ledger-scroll">
          <table className="ledger-table">
            <caption className="sr-only">Timestamped validation checks, thresholds, artifacts, and verdicts</caption>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Check</th>
                <th scope="col">Measured</th>
                <th scope="col">Policy</th>
                <th scope="col">Evidence</th>
                <th scope="col">Verdict</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td className="mono">{entry.timestamp}</td>
                  <th scope="row">{entry.check}</th>
                  <td className="mono ledger-measured">{entry.measured}</td>
                  <td className="mono">{entry.threshold}</td>
                  <td><ArtifactReference reference={entry.artifact} /></td>
                  <td>
                    <StatusBadge
                      label={entry.verdict.toUpperCase()}
                      tone={entry.verdict === "pass" ? "success" : entry.verdict === "fail" ? "danger" : "warning"}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-section">No validation measurements were returned.</div>
      )}
    </section>
  );
}

export function RecoveryTimeline({ entries }: { entries: TimelineEntry[] }) {
  return (
    <section className="panel timeline-panel" aria-labelledby="timeline-title">
      <SectionHeading
        eyebrow="WRITE-BACK & REVIEW"
        title="Recovery timeline"
        titleId="timeline-title"
        detail="PR creation is not recovery. The incident remains active until post-deploy proof."
      />
      {entries.length ? (
        <ol className="timeline">
          {entries.map((entry) => (
            <li className={`timeline-entry timeline-entry--${entry.state}`} key={entry.id}>
              <span className="timeline-entry__node" aria-hidden="true">
                {entry.state === "complete" ? <Check size={13} /> : entry.state === "failed" ? <X size={13} /> : <Clock3 size={13} />}
              </span>
              <time>{entry.timestamp}</time>
              <div>
                <strong>{entry.title}</strong>
                <p>{entry.detail}</p>
                {entry.artifact ? (
                  <ArtifactReference reference={entry.artifact} className="timeline-entry__artifact" />
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <div className="empty-section">No recovery events were returned.</div>
      )}
    </section>
  );
}

export function ContainmentPanel({ proof, active }: { proof: ContainmentProof; active: boolean }) {
  return (
    <section className={`panel containment-panel ${active ? "containment-panel--active" : ""}`} aria-labelledby="containment-title">
      <SectionHeading
        eyebrow={active ? "FAIL-CLOSED RESULT" : "FAIL-CLOSED ALTERNATIVE"}
        title={proof.title}
        titleId="containment-title"
        detail={proof.detail}
        action={active ? <StatusBadge label="Guard engaged" tone="danger" /> : <StatusBadge label="Policy enforced" tone="neutral" />}
      />
      <ul className="containment-actions">
        {proof.actions.map((action) => (
          <li key={action}>
            <ShieldAlert aria-hidden="true" size={16} />
            {action}
          </li>
        ))}
      </ul>
      <div className="guard-proof">
        <div>
          <Clipboard aria-hidden="true" size={15} />
          <code>{proof.guardCommand}</code>
        </div>
        <span>EXIT {proof.exitCode}</span>
      </div>
      {!active ? (
        <Link className="text-link" to="/cases/DR-025">
          Open containment evidence <ArrowRight aria-hidden="true" size={14} />
        </Link>
      ) : null}
    </section>
  );
}

export function DraftPrAction({ prUrl, onUnavailable }: { prUrl?: string; onUnavailable: () => void }) {
  if (prUrl) {
    return (
      <a className="primary-outline-button" href={prUrl} target="_blank" rel="noreferrer">
        <GitBranch aria-hidden="true" size={15} /> VIEW DRAFT PR <ExternalLink aria-hidden="true" size={13} />
      </a>
    );
  }
  return (
    <button className="primary-outline-button" onClick={onUnavailable} type="button">
      <GitBranch aria-hidden="true" size={15} /> VIEW DRAFT PR
    </button>
  );
}

export function DecisionSummaryIcon({ tone }: { tone: "warning" | "danger" | "success" | "context" | "neutral" }) {
  if (tone === "danger") return <ShieldAlert aria-hidden="true" size={24} />;
  if (tone === "success") return <FileCheck2 aria-hidden="true" size={24} />;
  return <Clock3 aria-hidden="true" size={24} />;
}
