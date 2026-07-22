import { containedReplayCase, replayCaseById, replayCases, replayPolicy } from "../data/replay";
import type {
  ApiEnvelope,
  Candidate,
  CaseState,
  CaseSummary,
  EvidenceItem,
  IntegrationStatus,
  PolicyDocument,
  RescueCase,
  Stage,
  TimelineEntry,
  ValidationEntry,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const PARSED_API_TIMEOUT = Number(import.meta.env.VITE_API_TIMEOUT_MS);
// A non-numeric override (e.g. "3500ms") would become NaN, and setTimeout(NaN)
// aborts on the first tick — silently forcing permanent replay fallback.
const API_TIMEOUT =
  Number.isFinite(PARSED_API_TIMEOUT) && PARSED_API_TIMEOUT > 0 ? PARSED_API_TIMEOUT : 3500;
const FORCE_REPLAY = import.meta.env.VITE_FORCE_REPLAY === "true";
const HOSTED_REPLAY_REASON =
  "manifest.json records SHA-256 digests for every included artifact";

type UnknownRecord = Record<string, unknown>;

class ApiHttpError extends Error {
  constructor(
    readonly status: number,
    statusText: string,
  ) {
    super(`API returned ${status} ${statusText}`.trim());
    this.name = "ApiHttpError";
  }
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): UnknownRecord {
  return isRecord(value) ? value : {};
}

function pick(record: UnknownRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

function stringValue(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.replace(/[%+,]/g, ""));
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function lowercaseValue(value: unknown, fallback = ""): string {
  return stringValue(value, fallback).toLowerCase();
}

function candidateExpression(value: unknown): string | undefined {
  const candidate = asRecord(value);
  const source = stringValue(pick(candidate, "source_field", "sourceField"));
  const alias = stringValue(pick(candidate, "target_alias", "targetAlias"), "revenue");
  return source ? `${source} AS ${alias}` : undefined;
}

function urnLabel(value: unknown): string {
  const urn = stringValue(value, "dataset");
  const tupleMatch = urn.match(/,([^,()]+),PROD\)?$/i);
  const identity = tupleMatch?.[1] ?? urn.split(":").at(-1) ?? urn;
  return identity.split(".").at(-1) ?? identity;
}

function schemaChangeTitle(item: UnknownRecord): string {
  const change = asRecord(item.schema_change);
  const before = arrayValue(change.before_fields).map((field) => stringValue(asRecord(field).name));
  const after = new Set(arrayValue(change.after_fields).map((field) => stringValue(asRecord(field).name)));
  const removed = before.find((field) => field && !after.has(field));
  if (!removed) return "Schema drift detected";
  return `${urnLabel(pick(item, "asset_urn", "entity_urn"))}.${removed} split detected`;
}

function unwrapPayload(raw: unknown): unknown {
  if (!isRecord(raw)) return raw;
  if (raw.data !== undefined) return raw.data;
  if (raw.case !== undefined) return raw.case;
  return raw;
}

function readableError(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return `API did not respond within ${API_TIMEOUT} ms`;
  }
  if (error instanceof Error) return error.message;
  return "API unavailable";
}

async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), API_TIMEOUT);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });

    if (!response.ok) {
      throw new ApiHttpError(response.status, response.statusText);
    }
    return await response.json();
  } finally {
    window.clearTimeout(timeout);
  }
}

function stringContainsReplayMarker(value: unknown): boolean {
  const normalized = stringValue(value).toUpperCase();
  return (
    normalized.includes("RECORDED_REPLAY") ||
    normalized.includes("RECORDED REPLAY") ||
    normalized.includes("ARTIFACT://REPLAY/")
  );
}

function integrationIsRecordedReplay(value: unknown): boolean {
  const integration = asRecord(value);
  const details = asRecord(integration.details);
  return (
    stringContainsReplayMarker(integration.status) ||
    stringContainsReplayMarker(details.fallback) ||
    stringContainsReplayMarker(details.evidence_mode)
  );
}

function candidateIsRecordedReplay(value: unknown): boolean {
  const candidate = asRecord(value);
  const build = asRecord(candidate.build);
  const reconciliation = asRecord(candidate.reconciliation);
  return (
    integrationIsRecordedReplay(candidate.integration) ||
    integrationIsRecordedReplay(build.integration) ||
    integrationIsRecordedReplay(reconciliation.integration) ||
    stringContainsReplayMarker(build.command) ||
    arrayValue(candidate.evidence_refs).some(stringContainsReplayMarker) ||
    arrayValue(build.evidence_refs).some(stringContainsReplayMarker) ||
    arrayValue(reconciliation.evidence_refs).some(stringContainsReplayMarker)
  );
}

function replaySignals(raw: unknown): string[] {
  const item = asRecord(unwrapPayload(raw));
  const context = asRecord(item.context);
  const schemaChange = asRecord(item.schema_change);
  const signals = new Set<string>();

  if (stringContainsReplayMarker(schemaChange.source)) signals.add("schema event");
  if (stringContainsReplayMarker(context.source) || integrationIsRecordedReplay(context.integration)) {
    signals.add("DataHub context");
  }
  if (integrationIsRecordedReplay(item.candidate_generation)) signals.add("candidate generation");
  if (arrayValue(item.candidates).some(candidateIsRecordedReplay)) signals.add("candidate validation");
  if (candidateIsRecordedReplay(item.selected_candidate)) signals.add("selected candidate");

  for (const rawEvent of arrayValue(item.events)) {
    const payload = asRecord(asRecord(rawEvent).payload);
    if (
      stringContainsReplayMarker(payload.execution_mode) ||
      stringContainsReplayMarker(payload.evidence_mode) ||
      integrationIsRecordedReplay(payload.integration) ||
      candidateIsRecordedReplay(payload.assessment)
    ) {
      signals.add("case event ledger");
    }
  }

  return [...signals];
}

function apiEnvelope<T>(data: T, raw: unknown): ApiEnvelope<T> {
  const signals = replaySignals(raw);
  if (signals.length) {
    return {
      data,
      mode: "replay",
      transport: "api",
      reason: `Backend provenance: RECORDED_REPLAY (${signals.join(", ")})`,
    };
  }
  return { data, mode: "live", transport: "api" };
}

function asCaseState(value: unknown): CaseState {
  const normalized = stringValue(value, "DETECTED").toUpperCase();
  const allowed: CaseState[] = [
    "DETECTED",
    "CONTEXT_GATHERED",
    "CANDIDATES_READY",
    "VALIDATING",
    "PATCH_READY",
    "PR_OPEN",
    "DEPLOYED",
    "POST_DEPLOY_VERIFIED",
    "RESOLVED",
    "CONTAINED",
    "FAILED",
  ];
  return allowed.includes(normalized as CaseState) ? (normalized as CaseState) : "DETECTED";
}

function stateLabel(state: CaseState): string {
  const labels: Record<CaseState, string> = {
    DETECTED: "Detected",
    CONTEXT_GATHERED: "Context gathered",
    CANDIDATES_READY: "Candidates ready",
    VALIDATING: "Validating",
    PATCH_READY: "Patch ready",
    PR_OPEN: "Awaiting human review",
    DEPLOYED: "Deployment observed",
    POST_DEPLOY_VERIFIED: "Post-deploy verified",
    RESOLVED: "Recovered",
    CONTAINED: "Contained",
    FAILED: "Failed",
  };
  return labels[state];
}

function normalizeSummary(raw: unknown): CaseSummary {
  const item = asRecord(raw);
  const context = asRecord(item.context);
  const selected = pick(item, "selectedCandidate", "selected_candidate");
  const state = asCaseState(pick(item, "state", "status"));
  const id = stringValue(pick(item, "id", "case_id", "caseId"), "UNKNOWN");
  return {
    id,
    title: stringValue(pick(item, "title", "headline", "change_summary"), schemaChangeTitle(item)),
    assetUrn: stringValue(pick(item, "assetUrn", "asset_urn", "entity_urn"), "URN not reported"),
    severity: stringValue(pick(item, "severity", "impact"), "High impact"),
    owner: stringValue(pick(item, "owner", "owner_name") ?? context.owner, "Unassigned"),
    state,
    stateLabel: stringValue(pick(item, "stateLabel", "state_label"), stateLabel(state)),
    updatedAt: stringValue(pick(item, "updatedAt", "updated_at", "observed_at"), new Date().toISOString()),
    incidentStatus: stringValue(pick(item, "incidentStatus", "incident_status"), "Unknown"),
    candidateCount: numberValue(pick(item, "candidateCount", "candidate_count"), arrayValue(item.candidates).length),
    selectedCandidate: stringValue(selected) || candidateExpression(selected),
  };
}

function defaultStages(state: CaseState): Stage[] {
  const sequence: Array<{ id: string; label: string; states: CaseState[] }> = [
    { id: "detected", label: "Detected", states: ["DETECTED"] },
    { id: "context", label: "Context", states: ["CONTEXT_GATHERED"] },
    { id: "candidates", label: "Candidates", states: ["CANDIDATES_READY"] },
    { id: "validation", label: "Validation", states: ["VALIDATING"] },
    { id: "patch", label: "Patch ready", states: ["PATCH_READY"] },
    { id: "review", label: "Draft PR", states: ["PR_OPEN"] },
    { id: "deploy", label: "Deploy", states: ["DEPLOYED", "POST_DEPLOY_VERIFIED"] },
    { id: "recovered", label: "Recovered", states: ["RESOLVED"] },
  ];
  const currentIndex = sequence.findIndex((item) => item.states.includes(state));
  return sequence.map((item, index) => ({
    id: item.id,
    label: item.label,
    status:
      state === "CONTAINED" || state === "FAILED"
        ? index < 4
          ? "complete"
          : index === 4
            ? "blocked"
            : "future"
        : index < currentIndex
          ? "complete"
          : index === currentIndex
            ? "current"
            : "future",
  }));
}

function normalizeStages(value: unknown, state: CaseState): Stage[] {
  const items = arrayValue(value);
  if (!items.length) return defaultStages(state);
  return items.map((raw, index) => {
    const item = asRecord(raw);
    const status = stringValue(item.status, "future");
    return {
      id: stringValue(item.id, `stage-${index}`),
      label: stringValue(item.label, `Stage ${index + 1}`),
      status: ["complete", "current", "future", "blocked"].includes(status)
        ? (status as Stage["status"])
        : "future",
      timestamp: stringValue(item.timestamp) || undefined,
    };
  });
}

function normalizeCandidates(value: unknown): Candidate[] {
  return arrayValue(value).map((raw, index) => {
    const item = asRecord(raw);
    const reconciliation = asRecord(item.reconciliation);
    const buildRecord = asRecord(item.build);
    const semantic = lowercaseValue(pick(item, "semanticVerdict", "semantic_verdict"), "unknown");
    const build = lowercaseValue(pick(item, "buildStatus", "build_status"));
    const outcome = lowercaseValue(pick(item, "decision", "outcome"), "pending");
    const failedChecks = arrayValue(pick(item, "policy_checks", "policyChecks"))
      .map(asRecord)
      .filter((check) => check.passed === false)
      .map(
        (check) =>
          `${stringValue(check.name, "Policy gate")}: ${stringValue(check.observed)}; requires ${stringValue(check.requirement)}`,
      );
    const buildStatus =
      build ||
      (typeof buildRecord.passed === "boolean"
        ? buildRecord.passed
          ? "passed"
          : "failed"
        : "not_run");
    const decision: Candidate["decision"] =
      outcome === "selected" ? "selected" : outcome === "rejected" ? "rejected" : "pending";
    return {
      id: stringValue(item.id, `candidate-${index}`),
      expression: stringValue(
        pick(item, "expression", "mapping", "sql_expression"),
        candidateExpression(item) ?? "Candidate not reported",
      ),
      semanticVerdict: ["match", "conflict", "unknown"].includes(semantic)
        ? (semantic as Candidate["semanticVerdict"])
        : "unknown",
      semanticDetail: stringValue(pick(item, "semanticDetail", "semantic_detail", "rationale"), "No semantic detail reported."),
      totalVariance: numberValue(
        pick(item, "totalVariance", "total_variance", "total_variance_pct") ??
          reconciliation.total_variance_pct,
      ),
      pkOverlap: numberValue(
        pick(item, "pkOverlap", "pk_overlap", "pk_overlap_pct") ??
          reconciliation.primary_key_overlap_pct,
      ),
      dbtPassed: numberValue(pick(item, "dbtPassed", "dbt_passed") ?? buildRecord.passed_checks),
      dbtTotal: numberValue(pick(item, "dbtTotal", "dbt_total") ?? buildRecord.total_checks),
      buildStatus: ["passed", "failed", "not_run"].includes(buildStatus)
        ? (buildStatus as Candidate["buildStatus"])
        : "not_run",
      decision,
      reason: stringValue(
        item.reason,
        failedChecks.join(" · ") ||
          (decision === "selected"
            ? "All deterministic gates passed."
            : "Decision reason not reported."),
      ),
      evidenceRefs: arrayValue(pick(item, "evidenceRefs", "evidence_refs")).map((ref) => stringValue(ref)),
    };
  });
}

function backendEvidence(item: UnknownRecord): EvidenceItem[] {
  const context = asRecord(item.context);
  if (!Object.keys(context).length) return [];
  const evidence: EvidenceItem[] = [];
  const glossary = stringValue(context.glossary_definition);
  const documents = arrayValue(context.context_documents).map((value) => stringValue(value));
  if (glossary) {
    evidence.push({
      id: "live-glossary",
      source: documents[0] || "DataHub glossary context",
      sourceType: "Glossary",
      claim: glossary,
      freshness: "Retrieved for this recovery run",
      verdict: "pass",
    });
  }
  const lineage = arrayValue(context.lineage_urns).map((value) => stringValue(value));
  if (lineage.length) {
    evidence.push({
      id: "live-lineage",
      source: lineage[0],
      sourceType: "Lineage",
      claim: `${lineage.length} current lineage assets were retrieved for impact analysis.`,
      freshness: context.lineage_current === true ? "Current at detection" : "Freshness unverified",
      verdict: context.lineage_current === true ? "pass" : "unknown",
    });
  }
  const owner = stringValue(context.owner);
  if (owner) {
    evidence.push({
      id: "live-owner",
      source: owner,
      sourceType: "Ownership",
      claim: `${owner} owns the affected data product and human review boundary.`,
      freshness: "Retrieved for this recovery run",
      verdict: "pass",
    });
  }
  const change = asRecord(item.schema_change);
  if (Object.keys(change).length) {
    evidence.push({
      id: "live-schema",
      source: stringValue(change.source, "schemaMetadata"),
      sourceType: "Schema",
      claim: schemaChangeTitle(item),
      freshness: stringValue(change.observed_at, "Observed for this recovery run"),
      verdict: "pass",
    });
  }
  return evidence;
}

function backendLineage(
  item: UnknownRecord,
  assetUrn: string,
): { nodes: RescueCase["lineageNodes"]; edges: RescueCase["lineageEdges"] } {
  const context = asRecord(item.context);
  const urns = [assetUrn, ...arrayValue(context.lineage_urns).map((value) => stringValue(value))].filter(
    (urn, index, all) => urn && all.indexOf(urn) === index,
  );
  const nodes = urns.map((urn, index) => ({
    id: `live-node-${index}`,
    label: urnLabel(urn),
    kind: index === 0 ? "Changed dataset" : "Downstream asset",
    status: (index === 0 ? "affected" : "healthy") as "affected" | "healthy",
    owner: index === 0 ? stringValue(context.owner, "Unassigned") : "DataHub lineage",
  }));
  const edges = nodes.slice(1).map((node, index) => ({
    source: nodes[index].id,
    target: node.id,
    relationship: "feeds",
  }));
  return { nodes, edges };
}

function backendLedger(selected: UnknownRecord): ValidationEntry[] {
  const checks = arrayValue(pick(selected, "policy_checks", "policyChecks")).map(asRecord);
  const refs = arrayValue(pick(selected, "evidence_refs", "evidenceRefs")).map((value) =>
    stringValue(value),
  );
  return checks.map((check, index) => ({
    id: `live-check-${index}`,
    timestamp: "This run",
    check: stringValue(check.name, `Gate ${index + 1}`),
    measured: stringValue(check.observed, "—"),
    threshold: stringValue(check.requirement, "—"),
    artifact: refs[index] || refs[0] || "Evidence reference unavailable",
    verdict: check.passed === true ? "pass" : check.passed === false ? "fail" : "unknown",
  }));
}

function backendTimeline(value: unknown): TimelineEntry[] {
  const events = arrayValue(value);
  return events.map((raw, index) => {
    const event = asRecord(raw);
    const eventType = stringValue(pick(event, "event_type", "eventType"), "EVENT");
    const state = asCaseState(event.state);
    return {
      id: stringValue(event.event_id, `live-event-${index}`),
      timestamp: stringValue(event.created_at, "—"),
      title: eventType.toLowerCase().replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase()),
      detail: `Case state: ${stateLabel(state)}.`,
      state:
        state === "FAILED"
          ? "failed"
          : state === "RESOLVED"
            ? "complete"
            : index === events.length - 1
              ? "current"
              : "complete",
    };
  });
}

function integrationState(value: unknown): IntegrationStatus["state"] {
  const status = lowercaseValue(asRecord(value).status);
  if (status === "succeeded") return "ready";
  if (status === "failed") return "degraded";
  if (status === "recorded_replay" || status === "not_run") return "pending";
  return "offline";
}

function backendIntegrations(item: UnknownRecord): IntegrationStatus[] {
  const context = asRecord(item.context);
  const selected = asRecord(item.selected_candidate);
  const build = asRecord(selected.build);
  const pullRequest = asRecord(item.pull_request);
  const datahubIntegration = pick(context, "integration") ?? item.incident_integration;
  const contextReplay =
    stringContainsReplayMarker(context.source) || integrationIsRecordedReplay(context.integration);
  const selectedReplay = candidateIsRecordedReplay(selected);
  return [
    {
      id: "datahub",
      label: "DATAHUB",
      state: contextReplay ? "pending" : integrationState(datahubIntegration),
      detail: contextReplay
        ? "RECORDED_REPLAY context"
        : stringValue(asRecord(datahubIntegration).message, "Not configured"),
    },
    {
      id: "postgres",
      label: "POSTGRES",
      state: selectedReplay ? "pending" : selected.id ? "ready" : "pending",
      detail: selectedReplay
        ? "RECORDED_REPLAY reconciliation"
        : selected.id
          ? "Candidate evidence recorded"
          : "Awaiting validation",
    },
    {
      id: "dbt",
      label: "DBT",
      state: selectedReplay
        ? "pending"
        : build.passed === true
          ? "ready"
          : build.passed === false
            ? "degraded"
            : "pending",
      detail:
        selectedReplay
          ? `RECORDED_REPLAY · ${numberValue(build.passed_checks)}/${numberValue(build.total_checks)} recorded`
          : build.passed === true
          ? `${numberValue(build.passed_checks)}/${numberValue(build.total_checks)} passed`
          : "Build not proven",
    },
    {
      id: "github",
      label: "GITHUB",
      state: integrationState(pullRequest.integration),
      detail: stringValue(asRecord(pullRequest.integration).message, "Human gate"),
    },
  ];
}

function normalizeEvidence(value: unknown): EvidenceItem[] {
  return arrayValue(value).map((raw, index) => {
    const item = asRecord(raw);
    const sourceType = stringValue(pick(item, "sourceType", "source_type"), "Context document");
    const allowedTypes: EvidenceItem["sourceType"][] = ["Glossary", "Lineage", "Ownership", "Schema", "Context document"];
    const verdict = stringValue(item.verdict, "unknown");
    return {
      id: stringValue(item.id, `evidence-${index}`),
      source: stringValue(pick(item, "source", "source_urn", "urn"), "Source not reported"),
      sourceType: allowedTypes.includes(sourceType as EvidenceItem["sourceType"])
        ? (sourceType as EvidenceItem["sourceType"])
        : "Context document",
      claim: stringValue(item.claim, "Claim not reported"),
      freshness: stringValue(pick(item, "freshness", "observed_at"), "Freshness not reported"),
      verdict: ["pass", "fail", "unknown"].includes(verdict)
        ? (verdict as EvidenceItem["verdict"])
        : "unknown",
    };
  });
}

function normalizeLedger(value: unknown): ValidationEntry[] {
  return arrayValue(value).map((raw, index) => {
    const item = asRecord(raw);
    const verdict = stringValue(item.verdict, "unknown");
    return {
      id: stringValue(item.id, `ledger-${index}`),
      timestamp: stringValue(item.timestamp, "—"),
      check: stringValue(item.check, "Unnamed check"),
      measured: stringValue(pick(item, "measured", "measured_value"), "—"),
      threshold: stringValue(item.threshold, "—"),
      artifact: stringValue(pick(item, "artifact", "artifact_ref"), "Not reported"),
      verdict: ["pass", "fail", "unknown"].includes(verdict)
        ? (verdict as ValidationEntry["verdict"])
        : "unknown",
    };
  });
}

function normalizeTimeline(value: unknown): TimelineEntry[] {
  return arrayValue(value).map((raw, index) => {
    const item = asRecord(raw);
    const state = stringValue(item.state, "future");
    return {
      id: stringValue(item.id, `timeline-${index}`),
      timestamp: stringValue(item.timestamp, "—"),
      title: stringValue(item.title, "Event"),
      detail: stringValue(item.detail, "No event detail reported."),
      state: ["complete", "current", "future", "failed"].includes(state)
        ? (state as TimelineEntry["state"])
        : "future",
      artifact: stringValue(item.artifact) || undefined,
    };
  });
}

function normalizeIntegrations(value: unknown): IntegrationStatus[] {
  return arrayValue(value).map((raw, index) => {
    const item = asRecord(raw);
    const id = stringValue(item.id, "datahub");
    const state = stringValue(item.state, "offline");
    return {
      id: (["datahub", "postgres", "dbt", "github"].includes(id) ? id : "datahub") as IntegrationStatus["id"],
      label: stringValue(item.label, `INTEGRATION ${index + 1}`),
      state: (["ready", "pending", "degraded", "offline"].includes(state) ? state : "offline") as IntegrationStatus["state"],
      detail: stringValue(item.detail, "Status not reported"),
    };
  });
}

function normalizeCase(raw: unknown, requestedId: string): RescueCase {
  const item = asRecord(unwrapPayload(raw));
  const summary = normalizeSummary({ ...item, id: pick(item, "id", "case_id") ?? requestedId });
  const state = summary.state;
  const context = asRecord(item.context);
  const path = arrayValue(pick(item, "affectedPath", "affected_path") ?? context.lineage_urns).map((part) =>
    urnLabel(part),
  );
  const lineage = asRecord(item.lineage);
  const patch = asRecord(pick(item, "patch", "sql_diff"));
  const selected = asRecord(pick(item, "selectedCandidate", "selected_candidate"));
  const selectedSource = stringValue(pick(selected, "source_field", "sourceField"));
  const explicitDiff = arrayValue(
    pick(item, "diff", "diff_lines", "sql_diff_lines", "lines") ?? patch.lines,
  );
  const rawDiff = explicitDiff.length
    ? explicitDiff
    : selectedSource
      ? [
          { number: 1, kind: "context", content: "select" },
          { number: 2, kind: "context", content: "  payment_id," },
          { number: 3, kind: "remove", content: "- amount AS revenue" },
          { number: 3, kind: "add", content: `+ ${selectedSource} AS revenue` },
        ]
      : [];
  const backendLineageData = backendLineage(item, summary.assetUrn);
  // Nodes and edges must come from the same source. Mixing API-provided nodes
  // with synthesized edges (or vice versa) yields edges whose endpoints do not
  // exist among the nodes, and ReactFlow silently drops every such edge.
  const providedLineageNodes = arrayValue(pick(item, "lineageNodes", "lineage_nodes") ?? lineage.nodes);
  const providedLineageEdges = arrayValue(pick(item, "lineageEdges", "lineage_edges") ?? lineage.edges);
  const useProvidedLineage = providedLineageNodes.length > 0;
  const rawLineageNodes = useProvidedLineage ? providedLineageNodes : backendLineageData.nodes;
  const rawLineageEdges = useProvidedLineage ? providedLineageEdges : backendLineageData.edges;
  const pullRequest = asRecord(item.pull_request);
  const pullRequestIntegration = asRecord(pullRequest.integration);
  const outcomeTone = state === "RESOLVED" ? "success" : state === "CONTAINED" || state === "FAILED" ? "danger" : "warning";

  return {
    ...summary,
    eyebrow: stringValue(item.eyebrow, `CASE ${summary.id}`),
    outcomeTitle: stringValue(
      pick(item, "outcomeTitle", "outcome_title"),
      state === "RESOLVED"
        ? "RECOVERED"
        : state === "CONTAINED"
          ? "AUTO-REPAIR REFUSED"
          : selectedSource
            ? "SAFE REPAIR VALIDATED"
            : "RECOVERY IN PROGRESS",
    ),
    outcomeDetail: stringValue(
      pick(item, "outcomeDetail", "outcome_detail"),
      state === "PR_OPEN"
        ? "Draft PR created — awaiting human review. Incident remains active."
        : selectedSource && lowercaseValue(pullRequestIntegration.status) === "not_run"
          ? "Safe patch validated. GitHub write was not run; incident remains active."
        : "Live case data returned by the DataRescue API.",
    ),
    outcomeTone,
    affectedPath: path,
    integrations: arrayValue(item.integrations).length
      ? normalizeIntegrations(item.integrations)
      : backendIntegrations(item),
    stages: normalizeStages(item.stages, state),
    lineageNodes: rawLineageNodes.map((rawNode, index) => {
      const node = asRecord(rawNode);
      const status = stringValue(node.status, "healthy");
      return {
        id: stringValue(node.id, `node-${index}`),
        label: stringValue(node.label, "Dataset"),
        kind: stringValue(node.kind, "Dataset"),
        status: (["healthy", "affected", "blocked"].includes(status) ? status : "healthy") as "healthy" | "affected" | "blocked",
        owner: stringValue(node.owner, "Unassigned"),
      };
    }),
    lineageEdges: rawLineageEdges.map((rawEdge) => {
      const edge = asRecord(rawEdge);
      return {
        source: stringValue(edge.source),
        target: stringValue(edge.target),
        relationship: stringValue(edge.relationship, "feeds"),
      };
    }),
    evidence: arrayValue(item.evidence).length ? normalizeEvidence(item.evidence) : backendEvidence(item),
    candidates: normalizeCandidates(item.candidates),
    diff: rawDiff.map((rawLine, index) => {
      const line = asRecord(rawLine);
      const kind = stringValue(line.kind, "context");
      return {
        number: numberValue(line.number, index + 1),
        kind: (["context", "remove", "add"].includes(kind) ? kind : "context") as "context" | "remove" | "add",
        content: stringValue(line.content, stringValue(rawLine)),
      };
    }),
    validationLedger: arrayValue(
      pick(item, "validationLedger", "validation_ledger", "ledger"),
    ).length
      ? normalizeLedger(pick(item, "validationLedger", "validation_ledger", "ledger"))
      : backendLedger(selected),
    timeline: arrayValue(item.timeline).length
      ? normalizeTimeline(item.timeline)
      : backendTimeline(item.events),
    containment: {
      title: stringValue(asRecord(item.containment).title, containedReplayCase.containment.title),
      detail: stringValue(asRecord(item.containment).detail, containedReplayCase.containment.detail),
      actions: arrayValue(asRecord(item.containment).actions).map((action) => stringValue(action)),
      guardCommand: stringValue(pick(asRecord(item.containment), "guardCommand", "guard_command"), containedReplayCase.containment.guardCommand),
      exitCode: numberValue(pick(asRecord(item.containment), "exitCode", "exit_code"), 75),
    },
    prLabel:
      stringValue(pick(item, "prLabel", "pr_label"), stringValue(pullRequest.branch)) ||
      undefined,
    prUrl: stringValue(pick(item, "prUrl", "pr_url") ?? pullRequest.url) || undefined,
  };
}

export async function fetchCases(): Promise<ApiEnvelope<CaseSummary[]>> {
  if (FORCE_REPLAY) {
    return {
      data: replayCases.map(({ stages: _stages, ...item }) => item),
      mode: "replay",
      transport: "hosted-replay",
      reason: HOSTED_REPLAY_REASON,
    };
  }
  try {
    const raw = await requestJson("/api/v1/cases");
    const payload = unwrapPayload(raw);
    const collection = Array.isArray(payload)
      ? payload
      : arrayValue(pick(asRecord(payload), "cases", "items", "results"));
    const data = collection.map(normalizeSummary);
    const replayCaseIds = collection
      .map((item, index) => (replaySignals(item).length ? data[index]?.id : undefined))
      .filter((id): id is string => Boolean(id));
    if (replayCaseIds.length) {
      return {
        data,
        mode: "replay",
        transport: "api",
        reason: `Backend provenance: RECORDED_REPLAY in ${replayCaseIds.join(", ")}`,
      };
    }
    return { data, mode: "live", transport: "api" };
  } catch (error) {
    return {
      data: replayCases.map(({ stages: _stages, ...item }) => item),
      mode: "replay",
      transport: "bundled-replay",
      reason: readableError(error),
    };
  }
}

export async function fetchCase(caseId: string): Promise<ApiEnvelope<RescueCase>> {
  if (FORCE_REPLAY) {
    const replayCase = replayCaseById(caseId);
    if (!replayCase) throw new Error(`Case ${caseId} was not found in the hosted replay package.`);
    return {
      data: replayCase,
      mode: "replay",
      transport: "hosted-replay",
      reason: HOSTED_REPLAY_REASON,
    };
  }
  try {
    const raw = await requestJson(`/api/v1/cases/${encodeURIComponent(caseId)}`);
    return apiEnvelope(normalizeCase(raw, caseId), raw);
  } catch (error) {
    if (error instanceof ApiHttpError && error.status === 404) {
      throw new Error(`Case ${caseId} was not found.`);
    }
    const replayCase = replayCaseById(caseId);
    if (!replayCase) {
      throw new Error(`Could not load case ${caseId}: ${readableError(error)}`);
    }
    return {
      data: replayCase,
      mode: "replay",
      transport: "bundled-replay",
      reason: readableError(error),
    };
  }
}

function policyFromThresholds(raw: UnknownRecord): PolicyDocument {
  const rules: PolicyDocument["rules"] = [
    {
      key: "semantic_evidence_required",
      label: "Semantic evidence",
      value: raw.semantic_evidence_required === true ? "Required" : "Optional",
      explanation: "A current business definition must support the mapping.",
      category: "Semantic",
    },
    {
      key: "max_total_variance_pct",
      label: "Total variance",
      value: `≤ ${numberValue(raw.max_total_variance_pct).toFixed(2)}%`,
      explanation: "Candidate total versus the last-good baseline.",
      category: "Reconciliation",
    },
    {
      key: "max_row_count_variance_pct",
      label: "Row-count variance",
      value: `≤ ${numberValue(raw.max_row_count_variance_pct).toFixed(2)}%`,
      explanation: "Candidate row count versus the last-good baseline.",
      category: "Reconciliation",
    },
    {
      key: "min_primary_key_overlap_pct",
      label: "Primary-key overlap",
      value: `≥ ${numberValue(raw.min_primary_key_overlap_pct).toFixed(2)}%`,
      explanation: "Existing payment identities must remain present.",
      category: "Reconciliation",
    },
    {
      key: "max_null_rate_delta_percentage_points",
      label: "Null-rate delta",
      value: `≤ ${numberValue(raw.max_null_rate_delta_percentage_points).toFixed(2)} pp`,
      explanation: "Null behavior may not materially deteriorate.",
      category: "Reconciliation",
    },
    {
      key: "dbt_build_required",
      label: "Isolated dbt build",
      value: raw.dbt_build_required === true ? "Required" : "Optional",
      explanation: "The candidate must execute in an isolated schema.",
      category: "Execution",
    },
    {
      key: "lineage_must_be_current",
      label: "Current lineage",
      value: raw.lineage_must_be_current === true ? "Required" : "Optional",
      explanation: "Impact evidence must be current.",
      category: "Context",
    },
  ];
  return {
    version: "datarescue-policy/live",
    updatedAt: new Date().toISOString(),
    rules,
    hardStops: [
      "Missing semantic evidence",
      "Stale lineage",
      "Any failed dbt build",
      "Any reconciliation threshold breach",
    ],
    automationBoundary: [
      "DataRescue may open a draft PR.",
      "Only a human may merge.",
      "Recovery requires post-deploy proof.",
    ],
    yaml: Object.entries(raw)
      .map(([key, value]) => `${key}: ${String(value)}`)
      .join("\n"),
  };
}

export async function fetchPolicy(): Promise<ApiEnvelope<PolicyDocument>> {
  if (FORCE_REPLAY) {
    return {
      data: replayPolicy,
      mode: "replay",
      transport: "hosted-replay",
      reason: HOSTED_REPLAY_REASON,
    };
  }
  try {
    const raw = asRecord(unwrapPayload(await requestJson("/api/v1/policy")));
    const rules = arrayValue(raw.rules);
    if (!rules.length) {
      return { data: policyFromThresholds(raw), mode: "live", transport: "api" };
    }
    return {
      data: {
        version: stringValue(raw.version, "datarescue-policy/live"),
        updatedAt: stringValue(pick(raw, "updatedAt", "updated_at"), new Date().toISOString()),
        rules: rules.map((rawRule) => {
          const rule = asRecord(rawRule);
          const category = stringValue(rule.category, "Context");
          return {
            key: stringValue(rule.key),
            label: stringValue(rule.label, stringValue(rule.key)),
            value: stringValue(rule.value),
            explanation: stringValue(rule.explanation),
            category: (["Semantic", "Reconciliation", "Execution", "Context"].includes(category)
              ? category
              : "Context") as "Semantic" | "Reconciliation" | "Execution" | "Context",
          };
        }),
        hardStops: arrayValue(pick(raw, "hardStops", "hard_stops")).map((item) => stringValue(item)),
        automationBoundary: arrayValue(pick(raw, "automationBoundary", "automation_boundary")).map((item) => stringValue(item)),
        yaml: stringValue(raw.yaml),
      },
      mode: "live",
      transport: "api",
    };
  } catch (error) {
    return {
      data: replayPolicy,
      mode: "replay",
      transport: "bundled-replay",
      reason: readableError(error),
    };
  }
}

export async function runDemoAction(action: "drift" | "reset"): Promise<void> {
  if (FORCE_REPLAY) {
    throw new Error("Live demo actions are disabled in the static RECORDED_REPLAY build");
  }
  await requestJson(`/api/v1/demo/${action}`, { method: "POST", body: "{}" });
}
