import { useQuery } from "@tanstack/react-query";
import { Ban, Check, Code2, LockKeyhole, ShieldCheck } from "lucide-react";
import { DataSourceBanner, ErrorPanel, LoadingPanel, SectionHeading, formatDateTime } from "../components/common";
import { fetchPolicy } from "../lib/api";

export function PolicyPage() {
  const query = useQuery({ queryKey: ["policy"], queryFn: fetchPolicy, retry: false });

  if (query.isLoading) {
    return <main className="page" id="main-content"><LoadingPanel label="Loading recovery policy" /></main>;
  }
  if (query.isError || !query.data) {
    return <main className="page" id="main-content"><ErrorPanel title="Policy unavailable" detail="The deterministic policy could not be loaded." /></main>;
  }
  const policy = query.data.data;

  return (
    <main className="page page--policy" id="main-content">
      <div className="page-heading">
        <div>
          <p className="eyebrow">DETERMINISTIC RECOVERY CONTRACT</p>
          <h1>Policy</h1>
          <p>The model proposes candidates. These gates make the decision.</p>
        </div>
        <div className="policy-version">
          <ShieldCheck aria-hidden="true" size={18} />
          <span><strong>{policy.version}</strong><small>Updated {formatDateTime(policy.updatedAt)}</small></span>
        </div>
      </div>
      <DataSourceBanner envelope={query.data} />

      <section className="panel policy-rules" aria-labelledby="policy-rules-title">
        <SectionHeading
          eyebrow="PASS / FAIL THRESHOLDS"
          title="Required evidence gates"
          titleId="policy-rules-title"
          detail="Unknown or stale evidence fails closed; it never counts as a pass."
        />
        <div className="policy-rule-grid">
          {policy.rules.map((rule) => (
            <article className="policy-rule" key={rule.key}>
              <div className="policy-rule__topline">
                <span>{rule.category}</span>
                <strong>{rule.value}</strong>
              </div>
              <h2>{rule.label}</h2>
              <p>{rule.explanation}</p>
              <code>{rule.key}</code>
            </article>
          ))}
        </div>
      </section>

      <div className="policy-grid">
        <section className="panel hard-stop-panel" aria-labelledby="hard-stops-title">
          <SectionHeading eyebrow="NON-NEGOTIABLE" title="Hard stops" titleId="hard-stops-title" detail="Any one condition refuses auto-repair." />
          <ul>
            {policy.hardStops.map((item) => <li key={item}><Ban aria-hidden="true" size={16} /><span>{item}</span></li>)}
          </ul>
        </section>
        <section className="panel boundary-panel" aria-labelledby="boundary-title">
          <SectionHeading eyebrow="HUMAN AUTHORITY" title="Automation boundary" titleId="boundary-title" detail="Proof enables review; it does not replace approval." />
          <ul>
            {policy.automationBoundary.map((item) => <li key={item}><LockKeyhole aria-hidden="true" size={16} /><span>{item}</span></li>)}
          </ul>
        </section>
      </div>

      <section className="panel yaml-panel" aria-labelledby="yaml-title">
        <SectionHeading eyebrow="SOURCE OF TRUTH" title="policy.yaml" titleId="yaml-title" detail="Read-only policy loaded by the backend worker." action={<span className="read-only-label"><Check aria-hidden="true" size={13} /> READ ONLY</span>} />
        <pre><Code2 aria-hidden="true" size={16} /><code>{policy.yaml}</code></pre>
      </section>
    </main>
  );
}
