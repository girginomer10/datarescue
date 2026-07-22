import {
  AlertTriangle,
  Check,
  CircleDashed,
  Clock3,
  CloudOff,
  Info,
  ShieldAlert,
  Wifi,
  X,
} from "lucide-react";
import type { ApiEnvelope, CaseState, Stage, Tone } from "../types";

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }).format(date) + " UTC";
}

export function toneForState(state: CaseState): Tone {
  if (state === "RESOLVED" || state === "POST_DEPLOY_VERIFIED") return "success";
  if (state === "CONTAINED" || state === "FAILED") return "danger";
  if (state === "DETECTED" || state === "VALIDATING" || state === "PR_OPEN" || state === "DEPLOYED") return "warning";
  return "context";
}

export function StatusBadge({ label, tone }: { label: string; tone: Tone }) {
  const Icon =
    tone === "success"
      ? Check
      : tone === "danger"
        ? ShieldAlert
        : tone === "warning"
          ? Clock3
          : tone === "context"
            ? Info
            : CircleDashed;
  return (
    <span className={`status-badge status-badge--${tone}`}>
      <Icon aria-hidden="true" size={13} strokeWidth={2.1} />
      {label}
    </span>
  );
}

export function DataSourceBanner<T>({ envelope }: { envelope: ApiEnvelope<T> }) {
  if (envelope.mode === "live") {
    return (
      <div className="source-banner source-banner--live" role="status">
        <Wifi aria-hidden="true" size={15} />
        <strong>LIVE API</strong>
        <span>Evidence and state are being read from DataRescue.</span>
      </div>
    );
  }

  return (
    <div className="source-banner source-banner--replay" role="status" aria-live="polite">
      <CloudOff aria-hidden="true" size={16} />
      <div>
        <strong>RECORDED_REPLAY EVIDENCE</strong>
        <span>
          {envelope.transport === "hosted-replay"
            ? `Static hosted demo. Hash-verified replay artifacts are served with this build${envelope.reason ? ` — ${envelope.reason}` : ""}. No API request was made.`
            : envelope.transport === "api"
            ? `API connected, but recorded workflow evidence is in use${envelope.reason ? ` — ${envelope.reason}` : ""}.`
            : `Live API unavailable${envelope.reason ? ` — ${envelope.reason}` : ""}. Bundled evidence is shown.`}{" "}
          No recorded result is presented as a live integration run.
        </span>
      </div>
    </div>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  titleId,
  detail,
  action,
}: {
  eyebrow?: string;
  title: string;
  titleId?: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h2 id={titleId}>{title}</h2>
        {detail ? <p>{detail}</p> : null}
      </div>
      {action ? <div className="section-heading__action">{action}</div> : null}
    </div>
  );
}

export function StageRail({ stages }: { stages: Stage[] }) {
  return (
    <nav className="stage-rail-wrap" aria-label="Recovery progress">
      <ol className="stage-rail">
        {stages.map((stage, index) => {
          const Icon = stage.status === "complete" ? Check : stage.status === "blocked" ? X : undefined;
          return (
            <li
              className={`stage stage--${stage.status}`}
              key={stage.id}
              aria-current={stage.status === "current" || stage.status === "blocked" ? "step" : undefined}
            >
              <div className="stage__rule" aria-hidden="true" />
              <span className="stage__node" aria-hidden="true">
                {Icon ? <Icon size={14} strokeWidth={2.5} /> : index + 1}
              </span>
              <span className="stage__copy">
                <strong>{stage.label}</strong>
                <span>
                  {stage.timestamp ??
                    (stage.status === "complete"
                      ? "Complete"
                      : stage.status === "blocked"
                        ? "Blocked"
                        : stage.status === "future"
                          ? "Pending"
                          : "In progress")}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export function LoadingPanel({ label = "Loading evidence" }: { label?: string }) {
  return (
    <div className="loading-panel" role="status">
      <CircleDashed aria-hidden="true" className="loading-panel__icon" size={21} />
      <span>{label}</span>
    </div>
  );
}

export function ErrorPanel({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="error-panel" role="alert">
      <AlertTriangle aria-hidden="true" size={20} />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}
