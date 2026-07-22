import {
  Activity,
  Database,
  GitBranch,
  Layers3,
  Menu,
  ShieldCheck,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

const shellIntegrations = [
  { label: "DATAHUB", state: "CONTEXT ROLE", icon: Layers3, tone: "context" },
  { label: "POSTGRES", state: "ISOLATION ROLE", icon: Database, tone: "neutral" },
  { label: "DBT", state: "VALIDATION ROLE", icon: Activity, tone: "neutral" },
  { label: "GITHUB", state: "HUMAN GATE", icon: GitBranch, tone: "warning" },
] as const;

function IntegrationItems() {
  return (
    <>
      {shellIntegrations.map(({ label, state, icon: Icon, tone }) => (
        <li className={`shell-integration shell-integration--${tone}`} key={label}>
          <Icon aria-hidden="true" size={14} strokeWidth={1.8} />
          <span className="shell-integration__label">{label}</span>
          <span className="shell-integration__state">{state}</span>
        </li>
      ))}
    </>
  );
}

export function AppShell() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <NavLink aria-label="DataRescue rescue queue" className="wordmark" to="/">
          <span className="wordmark__mark" aria-hidden="true">
            <ShieldCheck size={21} strokeWidth={1.8} />
            <span className="wordmark__pulse" />
          </span>
          <span>DATARESCUE</span>
          <span className="wordmark__edition">FORENSIC CONSOLE</span>
        </NavLink>

        <nav aria-label="Primary navigation" className="primary-nav">
          <NavLink to="/" end>
            Rescue queue
          </NavLink>
          <NavLink to="/policy">Policy</NavLink>
        </nav>

        <ul aria-label="Configured integration roles" className="shell-integrations shell-integrations--desktop">
          <IntegrationItems />
        </ul>

        <details className="integration-popover">
          <summary aria-label="Show integration roles">
            <Menu aria-hidden="true" size={19} />
            <span>Integrations</span>
          </summary>
          <ul aria-label="Configured integration roles" className="shell-integrations shell-integrations--popover">
            <IntegrationItems />
          </ul>
        </details>
      </header>
      <Outlet />
    </div>
  );
}
