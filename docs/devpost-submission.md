# DataRescue — Devpost submission copy

## Tagline

Prove the data fix before it ships.

## Elevator pitch

DataRescue turns a DataHub schema-drift event into an evidence-gated dbt repair. It
gathers lineage and business meaning, runs every candidate against isolated
PostgreSQL schemas, rejects fixes that merely compile, and opens a draft pull
request only when deterministic semantic, reconciliation, and dbt gates pass.

## Inspiration

Schema drift is rarely hard to detect. The dangerous part is deciding what a
renamed or split field now *means*. A technically valid replacement can silently
change revenue, customer balances, or regulatory reporting. We wanted a recovery
system that treats business context and real data as proof, not as decoration.

## What it does

The demo begins when `amount` is replaced by `gross_amount` and `net_amount` in a
PostgreSQL payments source. DataRescue:

1. accepts and deduplicates the DataHub schema-change event;
2. gathers schema, lineage, glossary, ownership, and document context;
3. proposes identifier-only mappings for the broken dbt revenue model;
4. runs each candidate in an isolated PostgreSQL schema with a real `dbt build`;
5. reconciles totals, row counts, primary keys, and null rates against last-good
   output;
6. deterministically rejects `gross_amount` despite a passing build because it
   conflicts with net-revenue semantics and changes revenue by `+3.40%`;
7. selects `net_amount` at `0.00%` variance, `100%` key overlap, and `8/8` dbt
   checks;
8. prepares or opens a real draft GitHub pull request while keeping the incident
   active; and
9. resolves the incident only after the merged commit is revalidated.

If no candidate passes every gate, DataRescue contains the case. Its guard command
blocks downstream execution and exits with code `75`.

## How we built it

- FastAPI, Pydantic, SQLite, and an append-only case state machine
- PostgreSQL 16 and dbt-postgres for isolated candidate execution
- DataHub MCL intake, MCP context boundaries, and GraphQL incident lifecycle
- OpenAI Responses API structured outputs for candidate proposals only
- A deterministic policy engine that owns every accept/reject decision
- Git worktrees and `gh pr create --draft` for bounded code changes
- React, TypeScript, TanStack Query, and React Flow for the Forensic Console

The language model cannot emit executable SQL or approve a repair. DataRescue
renders one allowlisted projection shape from validated identifiers, and every
important UI claim carries an evidence reference.

## Challenges

The hardest problem was separating “the code runs” from “the result preserves the
business contract.” Both demo candidates compile and pass structural dbt tests.
Only the combined glossary and reconciliation evidence exposes the convincing but
wrong gross-revenue repair.

We also designed every external boundary to fail honestly. Missing DataHub, MCP,
OpenAI, or GitHub configuration is shown as `NOT_CONFIGURED` or `NOT_RUN`; the
hosted console uses a hash-verified recorded evidence package and never labels it
as a live external action.

## Accomplishments

- A full drift-to-draft-PR vertical slice with a human merge boundary
- Exact, reproducible `+3.40%` versus `0.00%` candidate reconciliation
- Fail-closed containment with an executable downstream guard
- Idempotent MCL handling and append-only incident evidence
- Post-merge verification that recomputes evidence instead of trusting caller
  metrics
- A judge-readable Forensic Console where mint green is reserved for proven
  results

## What we learned

Metadata becomes operationally useful when it participates in a hard decision.
Lineage alone explains blast radius; glossary terms alone explain intent; dbt tests
alone explain technical validity. Recovery becomes defensible only when those
signals are joined with real-data reconciliation and an explicit policy.

## What's next

The first release stays deliberately narrow: PostgreSQL, dbt, DataHub, and GitHub.
Next steps are production-grade job isolation, signed evidence artifacts, richer
DataHub context contracts, and additional warehouses after the PostgreSQL safety
model is proven in real deployments.

## Built with

Python · FastAPI · Pydantic · SQLite · PostgreSQL · dbt · DataHub · OpenAI
Responses API · React · TypeScript · Vite · TanStack Query · React Flow · GitHub
CLI · Docker

## Honest demo modes

- `make demo`: real PostgreSQL/dbt evidence with clearly labelled recorded
  DataHub context, designed for a reliable local judging run.
- `make demo-connected`: fail-fast connected path for a configured DataHub Core,
  Actions consumer, MCP endpoint, and GitHub credentials.
- Hosted console: hash-verified recorded evidence; no external mutation is
  represented as live.
