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
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
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
checks live MCP context, starts the API in `replay=false` / `postgres` mode,
waits for the pinned DataHub Actions consumer to subscribe, and only then
ingests the drift. It refuses to start without all of the following:

- `DATARESCUE_DATAHUB_MCP_URL` for live schema, lineage, glossary, ownership, and document context.
- `DATARESCUE_OPENAI_API_KEY` (or `OPENAI_API_KEY`) for structured candidate proposals.
- An authenticated `gh` CLI (optionally `GH_TOKEN` / `GITHUB_TOKEN`) and
  `DATARESCUE_GITHUB_REPOSITORY` for a real draft PR.
- Reachable Kafka and Schema Registry endpoints. The official pinned Quickstart
  defaults are `127.0.0.1:9092` and `http://127.0.0.1:8081`; override
  `DATAHUB_KAFKA_BOOTSTRAP` and `DATAHUB_SCHEMA_REGISTRY_URL` explicitly for any
  other deployment.

DataRescue does not guess Docker networks or container names. Validate the exact
Actions v1.6.0.15 recipe and custom action import without starting a consumer:

```bash
make datahub-actions-validate
```

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
