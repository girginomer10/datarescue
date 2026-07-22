# DataRescue

> Prove the fix before it ships.

DataRescue is an evidence-gated runtime recovery agent for DataHub and dbt. It detects schema drift, gathers semantic context, validates candidate repairs against real PostgreSQL data, and opens a draft pull request only when deterministic policy gates pass.

The demo is intentionally adversarial: both candidate mappings compile, but `gross_amount AS revenue` is rejected because it conflicts with the business glossary and changes recognized revenue by **+3.40%**. `net_amount AS revenue` is selected only after semantic, reconciliation, and dbt checks pass.

- [Open the hosted, hash-verified evidence replay](https://girginomer10.github.io/datarescue/)
- [Inspect the application-generated draft PR](https://github.com/girginomer10/datarescue/pull/1)

The hosted replay is credential-free and remains explicitly labeled as recorded evidence. The linked draft PR is separate live proof of the GitHub write path; it remains open for human review and is not presented as recovery.

## What makes it different

- Runs candidate repairs in isolated PostgreSQL schemas instead of trusting generated code.
- Separates technical validity from business correctness.
- Keeps the DataHub incident active while a draft PR awaits human review.
- Revalidates the merged revision before declaring recovery.
- Fails closed and can block a downstream command when no safe repair exists.
- Labels missing integrations as `NOT_CONFIGURED` or `NOT_RUN`; it never fakes external success.

## Quick start

Requirements:

- A running Docker engine (Docker Desktop, Colima, or equivalent)
- [uv](https://docs.astral.sh/uv/) (the repository pins and provisions Python 3.11)
- Node.js 22+

```bash
cp .env.example .env
make install
make demo
```

Then open:

- Forensic Console: <http://127.0.0.1:5173>
- API documentation: <http://127.0.0.1:8000/docs>

`make demo` uses recorded DataHub context while executing both candidates against
real PostgreSQL with real dbt builds. It needs no DataHub, OpenAI, or GitHub
credentials, and labels every external operation that was not run. Use
`make demo-replay` only for the fastest all-recorded UI path.

## Demo controls

```bash
make demo-drift       # apply amount → gross_amount + net_amount
make demo-data-reset  # restore the healthy amount fixture
make test-demo        # run both candidates against PostgreSQL/dbt
make datahub-mcl-proof # real Kafka MCL → live incident → restart-safe dedup
make datahub-mcp-proof # official MCP reads + real DataHub document write-back
make test             # backend, frontend, and dbt integration suites
make lint             # Python checks and frontend type/build checks
make check            # lint, tests, integrations, and production build
```

Circuit breaker example:

```bash
uv run datarescue guard \
  --asset 'urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)' \
  -- echo 'downstream started'
```

A contained asset exits with status `75` without running the downstream command.

## Product flow

```text
DETECTED
→ CONTEXT_GATHERED
→ CANDIDATES_READY
→ VALIDATING
→ PATCH_READY
→ PR_OPEN
→ DEPLOYED
→ POST_DEPLOY_VERIFIED
→ RESOLVED
```

`CONTAINED` and `FAILED` are explicit branches. A draft PR is not recovery.

## API

```text
POST /api/v1/events/schema-change
POST /api/v1/events/datahub-mcl
GET  /api/v1/cases
GET  /api/v1/cases/{id}
GET  /api/v1/cases/{id}/events
POST /api/v1/cases/{id}/verify-deployment
GET  /api/v1/policy
POST /api/v1/demo/drift
POST /api/v1/demo/reset
```

## Repository map

```text
apps/api/              FastAPI application, worker, store, and CLI
apps/web/              React/Vite Forensic Console
packages/datahub/      MCP, MCL, and GraphQL integration boundaries
packages/policy/       Deterministic policy engine
packages/remediation/  Candidate, SQL safety, git, and PR workflow
packages/evidence/     Reconciliation metrics and artifacts
demo/postgres/         Healthy and drifted fixture data
demo/dbt/              dbt-postgres models and tests
demo/datahub/          Ingestion recipes
demo/replay/           Hash-verified hosted evidence package
docs/                  Architecture, demo, and Devpost materials
```

## Connected integrations

The fail-fast connected path is:

```bash
make demo-connected
```

It starts the pinned DataHub v1.6 Quickstart, ingests the healthy baseline,
seeds and verifies the exact glossary/owner/document/lineage contract, starts
the official `mcp-server-datahub==0.6.0` on loopback, checks live MCP context,
starts the API in `replay=false` / `postgres` mode, waits for the pinned DataHub
Actions consumer to subscribe, and only then ingests the drift. A bounded
verifier then requires exactly one proof-owned MCL case to reach `PR_OPEN` with
the DataHub incident still remotely `ACTIVE`; containment, failure, timeout, or
incomplete evidence makes the command fail. The launcher does not merge the
draft PR, deploy it, or claim incident resolution. Set
`DATARESCUE_DATAHUB_MCP_URL` only to use an externally managed HTTPS MCP
server, with a separate `DATARESCUE_DATAHUB_MCP_TOKEN` when needed. DataHub GMS
credentials are never sent to that server. Otherwise the launcher owns
`http://127.0.0.1:8001/mcp` for the run. It refuses to start without all of the
following:

- `DATARESCUE_OPENAI_API_KEY` (or `OPENAI_API_KEY`) for structured candidate proposals.
- An authenticated `gh` CLI (optionally `GH_TOKEN` / `GITHUB_TOKEN`) and
  `DATARESCUE_GITHUB_REPOSITORY` for a real draft PR.
- Reachable Kafka and Schema Registry endpoints. DataRescue maps the pinned
  Quickstart GMS to `127.0.0.1:18080`; Kafka is `127.0.0.1:9092` and the
  v1.6 internal Schema Registry is proxied at
  `http://127.0.0.1:18080/schema-registry/api/`. Override
  `DATAHUB_KAFKA_BOOTSTRAP` and `DATAHUB_SCHEMA_REGISTRY_URL` explicitly for any
  other deployment.

DataRescue does not guess Docker networks or container names. Validate the exact
Actions v1.6.0.15 recipe and custom action import without starting a consumer:

```bash
make datahub-actions-validate
```

Two bounded proofs exercise the connected DataHub substrate without OpenAI or
GitHub credentials:

```bash
make datahub-mcl-proof
make datahub-mcp-proof
```

The first proves automatic Kafka MCL intake, a real incident mutation followed
by an exact `incidentInfo` read-back of remote `ACTIVE` state, fail-closed
containment, and durable deduplication after API restart. The second proves
official MCP `get_entities`, `list_schema_fields`, `get_lineage`, and
`save_document` calls against the live catalog. Document persistence is checked
independently through DataHub GMS `documentInfo`, including the exact saved
title, content, and related asset; the MCP response alone is not accepted as
proof. These bounded proofs do not claim the OpenAI or draft-PR stages ran.
Stop the retained infrastructure with `make datahub-down` and
`make postgres-down` when finished.

Tokens should be least-privilege and scoped to allowlisted assets/repositories. DataHub-provided SQL and other metadata are untrusted and are never executed directly.

## Design

The Forensic Console uses a dark, high-contrast operational system. Mint is reserved for proven success; amber means incomplete proof or human action; coral means a rejected gate or containment. Every critical state includes text and an icon rather than relying on color alone.

The design source and implementation tokens live in `.superdesign/design-system.md`.
The reviewed Case Detail composition is available on the
[DataRescue Superdesign canvas](https://superdesign.dev/teams/bae29233-90f4-4136-9b05-6e06189b4103/projects/c5f3ea70-9552-41e5-aeaf-7d911f2b1608).

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [2:50 demo script](docs/demo-script.md)
- [Devpost submission copy](docs/devpost-submission.md)
- [Recorded evidence package](demo/replay/README.md)
- [DataHub integration guide](demo/datahub/README.md)
- [Completion definition and resume order](memory/decisions/datarescue-hackathon-definition-of-done.md)

## License

Apache-2.0
