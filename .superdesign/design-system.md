# DataRescue Forensic Console Design System

## Product context

DataRescue is an evidence-gated runtime recovery agent for DataHub and dbt. Its primary user is an on-call data/platform engineer deciding whether a schema-drift repair is safe to ship. The interface must let a hackathon judge understand the core proof in seconds: a technically valid but semantically wrong candidate is rejected, a correct candidate is validated, and recovery remains human-approved.

Primary pages:

1. Rescue Queue: active and completed incidents.
2. Case Detail: lineage, evidence, candidate comparison, patch, validation ledger, and recovery timeline.
3. Policy: the deterministic gates that govern automation.

Core message: **Prove the fix before it ships.**

## Art direction

Use a high-contrast, typography-first Swiss operational aesthetic adapted into a dark forensic console. The UI is sober, precise, and information-dense without feeling cramped. Sharp alignment, thin borders, tabular evidence, short uppercase labels, and deliberate whitespace carry the visual identity. Avoid cyberpunk decoration, purple gradients, glassmorphism, generic AI sparkle icons, chat bubbles, oversized marketing heroes, and meaningless charts.

The dramatic visual moment is the candidate decision matrix: `gross_amount` compiles but is rejected with a +3.40% variance; `net_amount` is selected with 0.00% variance and 8/8 passing tests.

## Color tokens

- Canvas: `#0C1117`
- Elevated surface: `#111923`
- Secondary surface: `#16212D`
- Hairline border: `#263140`
- Strong border: `#354356`
- Primary text: `#E6EDF3`
- Muted text: `#9DA7B3`
- Dim text: `#6F7B89`
- Context cyan: `#4CC9F0`
- Warning amber: `#F5B942`
- Rejection coral: `#F97066`
- Proven mint: `#45D6A1`
- White highlight: `#F8FAFC`

Mint must only communicate evidence-backed success. Amber communicates pending human action or incomplete proof. Coral communicates failed gates and containment. Cyan identifies DataHub-derived context and neutral active controls.

## Typography

- UI and headings: Inter, system fallback `-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.
- Identifiers, URNs, SQL, hashes, metrics, and timestamps: JetBrains Mono, `SFMono-Regular, Consolas, monospace`.
- Page title: 30px/36px, weight 680, tracking -0.03em.
- Section title: 16px/22px, weight 650.
- Body: 15px/22px, weight 430.
- Table: 13px/18px, weight 500.
- Eyebrow/status labels: 11px/14px, weight 700, uppercase, tracking 0.09em.
- Numbers that decide a gate: 18px/24px, tabular numerals.

## Grid, spacing, and shape

- Desktop demo viewport: 1440x900; content max-width 1500px.
- App shell: 72px header, 24px outer gutters, 20px vertical rhythm.
- Base spacing scale: 4, 8, 12, 16, 20, 24, 32, 40px.
- Cards: 10px radius, 1px hairline border, no floating drop shadows.
- Buttons: 8px radius, 36px height; primary controls use cyan text/border on a dark surface, not filled gradients.
- Chips: 999px radius only for compact status metadata.
- Tables use horizontal rules, 12px cell padding, and no alternating zebra color.

## App shell

Header contains the DataRescue wordmark and pulse mark on the left, primary navigation in the center, and integration health on the right: DATAHUB, POSTGRES, DBT, GITHUB. Each integration uses an icon, text label, and explicit state; color is never the only state signal.

The product has no persistent left sidebar. Case Detail needs maximum horizontal space for lineage and evidence. Navigation uses a restrained horizontal header with active-route underline.

## Core components

### Status badge

Always includes icon + label. States: Detected, Gathering context, Validating, Awaiting review, Contained, Recovered, Failed. Do not show green for `PR_OPEN`; it is amber and labelled `Awaiting human review`.

### Stage rail

Horizontal sequence with numbered nodes and connecting rules. Completed steps show a check and timestamp; current step has an outer cyan/amber ring; future steps are dim. On narrow screens it becomes a vertical list.

### Evidence card

Compact surface with source type, claim, source URN/link, freshness timestamp, and pass/fail/unknown verdict. DataHub-sourced items use a cyan left rule. Unknown or stale evidence is amber, never silently treated as valid.

### Candidate decision matrix

Full-width comparison table. Rows are candidates and columns are Semantic evidence, Total variance, PK overlap, dbt build, and Decision. The rejected `gross_amount` row has a coral decision cell and explicit reason. The selected `net_amount` row has a mint left rule and `SELECTED` label. Technical pass and final policy decision are visually separate.

### Validation ledger

An append-only-looking list with timestamps, check name, measured value, threshold, source artifact, and verdict. It resembles an audit log rather than a generic progress checklist.

### SQL diff

Monospaced two-column or unified diff with line numbers. Removed fields use subtle coral background; added fields use subtle mint background. Copy and open-artifact actions remain visible.

### Lineage graph

Simple left-to-right nodes and edges with the affected dataset emphasized in amber. Provide a tabular equivalent immediately below or via a visible `Table view` toggle.

## Motion and interaction

- Standard transition: 180ms ease-out.
- Expansion/drawer: 220ms cubic-bezier(0.2, 0.8, 0.2, 1).
- Use one brief lineage pulse when a drift is triggered.
- Live event additions fade/translate by no more than 6px.
- Respect `prefers-reduced-motion`; disable pulse and transforms.
- Never animate critical numbers while a judge is trying to compare them.

## Accessibility

- Minimum body size 15px; important controls 16px.
- Maintain WCAG AA contrast.
- Every icon button has an accessible label.
- Focus rings use a 2px cyan outline with 2px offset.
- Candidate decision, incident state, and integration status are not color-only.
- Announce state changes through `aria-live="polite"`.
- All tables have semantic headers and descriptive captions.
- Lineage has a keyboard-accessible table equivalent.

## Responsive behavior

- Desktop is the primary judging experience.
- At widths below 1000px, split panels stack, candidate table becomes horizontally scrollable, and integration status collapses to a popover.
- At widths below 700px, stage rail becomes vertical and dense secondary metadata hides behind disclosure controls.

## Required Case Detail content

- `CASE DR-024` and `payments_raw.amount split detected`.
- Severity `High impact`, owner `Finance Data`.
- Affected path: `payments_raw → stg_payments → fct_revenue → executive_revenue`.
- Glossary definition stating recognized revenue excludes processing fees.
- Candidate `gross_amount AS revenue`: semantic conflict, +3.40% variance, 100% PK, dbt passed, rejected.
- Candidate `net_amount AS revenue`: semantic match, 0.00% variance, 100% PK, dbt 8/8, selected.
- Merge-before state: `SAFE REPAIR VALIDATED`, `Draft PR created — awaiting human review`, incident active.
- Post-merge state: `RECOVERED`, build passed, incident resolved, degraded tag cleared, evidence report written.
- Containment alternative: `AUTO-REPAIR REFUSED`, no candidate satisfies policy, incident raised, downstream blocked.

## Copy tone

Use short, factual operational language. Prefer `Rejected: revenue variance exceeds 0.50% policy` over conversational AI narration. Do not claim notification, deployment, or recovery unless the corresponding integration event exists.
