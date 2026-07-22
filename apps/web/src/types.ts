export type DataMode = "live" | "replay";
export type DataTransport = "api" | "bundled-replay" | "hosted-replay";

export type CaseState =
  | "DETECTED"
  | "CONTEXT_GATHERED"
  | "CANDIDATES_READY"
  | "VALIDATING"
  | "PATCH_READY"
  | "PR_OPEN"
  | "DEPLOYED"
  | "POST_DEPLOY_VERIFIED"
  | "RESOLVED"
  | "CONTAINED"
  | "FAILED";

export type Tone = "neutral" | "context" | "warning" | "danger" | "success";

export interface ApiEnvelope<T> {
  data: T;
  mode: DataMode;
  transport: DataTransport;
  reason?: string;
}

export interface CaseSummary {
  id: string;
  title: string;
  assetUrn: string;
  severity: string;
  owner: string;
  state: CaseState;
  stateLabel: string;
  updatedAt: string;
  incidentStatus: string;
  candidateCount: number;
  selectedCandidate?: string;
}

export interface Stage {
  id: string;
  label: string;
  status: "complete" | "current" | "future" | "blocked";
  timestamp?: string;
}

export interface LineageNodeData {
  id: string;
  label: string;
  kind: string;
  status: "healthy" | "affected" | "blocked";
  owner: string;
}

export interface LineageEdgeData {
  source: string;
  target: string;
  relationship: string;
}

export interface EvidenceItem {
  id: string;
  source: string;
  sourceType: "Glossary" | "Lineage" | "Ownership" | "Schema" | "Context document";
  claim: string;
  freshness: string;
  verdict: "pass" | "fail" | "unknown";
}

export interface Candidate {
  id: string;
  expression: string;
  semanticVerdict: "match" | "conflict" | "unknown";
  semanticDetail: string;
  totalVariance: number;
  pkOverlap: number;
  dbtPassed: number;
  dbtTotal: number;
  buildStatus: "passed" | "failed" | "not_run";
  decision: "selected" | "rejected" | "pending";
  reason: string;
  evidenceRefs: string[];
}

export interface DiffLine {
  number: number;
  kind: "context" | "remove" | "add";
  content: string;
}

export interface ValidationEntry {
  id: string;
  timestamp: string;
  check: string;
  measured: string;
  threshold: string;
  artifact: string;
  verdict: "pass" | "fail" | "unknown";
}

export interface TimelineEntry {
  id: string;
  timestamp: string;
  title: string;
  detail: string;
  state: "complete" | "current" | "future" | "failed";
  artifact?: string;
}

export interface ContainmentProof {
  title: string;
  detail: string;
  actions: string[];
  guardCommand: string;
  exitCode: number;
}

export interface IntegrationStatus {
  id: "datahub" | "postgres" | "dbt" | "github";
  label: string;
  state: "ready" | "pending" | "degraded" | "offline";
  detail: string;
}

export interface RescueCase extends CaseSummary {
  eyebrow: string;
  outcomeTitle: string;
  outcomeDetail: string;
  outcomeTone: Tone;
  affectedPath: string[];
  integrations: IntegrationStatus[];
  stages: Stage[];
  lineageNodes: LineageNodeData[];
  lineageEdges: LineageEdgeData[];
  evidence: EvidenceItem[];
  candidates: Candidate[];
  diff: DiffLine[];
  validationLedger: ValidationEntry[];
  timeline: TimelineEntry[];
  containment: ContainmentProof;
  prLabel?: string;
  prUrl?: string;
}

export interface PolicyRule {
  key: string;
  label: string;
  value: string;
  explanation: string;
  category: "Semantic" | "Reconciliation" | "Execution" | "Context";
}

export interface PolicyDocument {
  version: string;
  updatedAt: string;
  rules: PolicyRule[];
  hardStops: string[];
  automationBoundary: string[];
  yaml: string;
}
