# DataHub integration recipes

DataRescue does not embed an unofficial, brittle clone of DataHub's full stack
in this Compose file. Run DataHub Core with its supported quickstart or point at
an existing GMS, then use the optional ingestion runners:

```bash
make datahub-up
make datahub-check
make datahub-ingest
```

Or run the complete connected path in one command:

```bash
make demo-connected
```

This installs the pinned DataHub CLI, starts the official v1.6.0 quickstart,
generates and ingests both metadata sources, seeds the exact semantic context,
starts the official `mcp-server-datahub==0.6.0`, and launches the app with
`DATARESCUE_REPLAY_MODE=false` and `DATARESCUE_EXECUTION_MODE=postgres`.
DataHub's quickstart itself requires Docker Compose v2.

The command is deliberately fail-fast. Before starting the heavy stack it
requires an OpenAI key, authenticated `gh` CLI, and an accessible repository.
An environment token is optional when `gh` can use its credential store. By
default it manages the MCP server at `http://127.0.0.1:8001/mcp`; set
`DATARESCUE_DATAHUB_MCP_URL` only for an external HTTPS server and use the
separate `DATARESCUE_DATAHUB_MCP_TOKEN` if that server requires authentication.
The GMS token is never reused as MCP authentication. After baseline ingestion
the launcher verifies the semantic seed and calls the MCP context tools for the
monitored URN; any incomplete context stops the run. After drift ingestion, a
bounded case verifier requires exactly one proof-owned MCL case to reach
`PR_OPEN`, including the rejected `gross_amount` candidate, selected
`net_amount` candidate, active incident, verified evidence write-back, and real
draft-PR reference. It does not merge, deploy, or resolve the incident.

Run the credential-free connected proofs independently:

```bash
make datahub-mcl-proof
make datahub-mcp-proof
```

`datahub-mcl-proof` creates one automatic MCL case and a real active incident,
reads its remote `ACTIVE` state back through DataHub `incidentInfo`, then
rewinds the same Kafka consumer range after an API restart and proves no second
case is created. `datahub-mcp-proof` verifies the official read tools and a real
`save_document` evidence write-back, then uses DataHub GMS `documentInfo` to
check the exact persisted title, content, and related asset. Both intentionally
stop before the OpenAI/GitHub stages.

## Real-time MCL consumer

`schema-drift-actions.yml` follows the `acryl-datahub-actions==1.6.0.15` config
schema and is launched with the version-verified CLI form:

```bash
datahub-actions --debug actions run -c demo/datahub/schema-drift-actions.yml
```

The command and config fields were checked against the
[published 1.6.0.15 package](https://pypi.org/project/acryl-datahub-actions/1.6.0.15/)
and its matching official
[Actions source](https://github.com/datahub-project/datahub/tree/57fa40fef3ce8b5d1aad3c039c59471dd541b020/datahub-actions/src/datahub_actions).

The Kafka source reads only `MetadataChangeLog_Versioned_v1`, and the
`event_type` filter passes only dataset `schemaMetadata` MCLs to
`packages.datahub.actions:DataRescueSchemaAction`. Synchronous offset commits,
API event deduplication, and three retries make redelivery safe. The extended
Kafka poll interval allows real dbt/PostgreSQL validation to finish before the
consumer must poll again. Before drift ingestion, the launcher queries Kafka
and requires an assigned member in the `datarescue-schema-drift` consumer
group; a process-started log line alone is not treated as readiness.

The connected launcher enforces this order:

```text
healthy DataHub baseline
→ verified glossary, owner, document, and dbt lineage
→ official loopback MCP ready
→ live MCP check
→ proof-owned API healthy in postgres mode
→ Actions consumer subscribed
→ apply database drift
→ ingest changed schema / emit MCL
→ bounded evidence and candidate verification
→ draft PR open; incident still remotely ACTIVE
```

The API gate rejects an already occupied port and verifies a per-run nonce plus
the intended state-database identity. dbt candidate and DataHub ingestion runs
use separate target directories. Each DataHub ingestion receives one unique,
complete artifact snapshot that is held unchanged and mounted read-only for the
entire consumer run, so a concurrent dbt invocation cannot replace its
manifest, catalog, or run result.

The official pinned Quickstart publishes Kafka on `127.0.0.1:9092` and serves
its internal Schema Registry through GMS. DataRescue maps GMS to port `18080`
and therefore uses `http://127.0.0.1:18080/schema-registry/api/`. It does not
inspect or guess Docker networks/container names. For an external or remapped
DataHub installation, set both endpoints explicitly:

```bash
export DATAHUB_KAFKA_BOOTSTRAP=kafka.example.internal:9092
export DATAHUB_SCHEMA_REGISTRY_URL=https://schema-registry.example.internal
```

Validate the YAML schema, source/filter registries, and dotted custom action
import without connecting to Kafka:

```bash
make datahub-actions-validate
```

The runners use `acryldata/datahub-ingestion:v1.6.0` by default. Override
`DATAHUB_INGESTION_IMAGE` to match the target DataHub installation.

Both recipes deliberately share the identity settings required for URN parity:

| Setting | PostgreSQL recipe | dbt recipe |
| --- | --- | --- |
| Environment | `PROD` | `PROD` |
| Lower-case URNs | `true` | `true` |
| Platform instance | Unset | Unset |
| Platform | `postgres` | `target_platform: postgres` |
| Database in name | Native PostgreSQL name | `include_database_name: true` |

For the monitored fixture, both sources resolve to:

```text
urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)
```

The dbt recipe requires `manifest.json`, `catalog.json`, and `run_results.json`.
`make dbt-artifacts` generates all three before ingestion. Documentation
generation uses an isolated target and publishes only `manifest.json` and
`catalog.json`, so its synthetic run result cannot overwrite the successful
`dbt build` result consumed by DataHub.
The recipe reads their directory from `DBT_ARTIFACT_ROOT`; the container runner
sets it to the immutable `/artifacts` snapshot automatically.

Candidate acceptance follows the same provenance rule. Reconciliation reads
the exact `<candidate_schema>.fct_revenue` relation built by dbt, never a
separate hand-rendered query. An exit code of zero is insufficient: a missing
or malformed `run_results.json`, a result that is not `dbt build`, missing
expected models or tests, or any non-success result fails closed.

The semantic seeder is idempotent and fail-closed:

```bash
make datahub-seed-context
make datahub-verify-context
```

It creates `NetRevenue`, the deterministic `Finance Data` owner, and
`urn:li:document:datarescue-net-revenue-contract`, but never manufactures dbt
lineage. Verification requires four semantic edges: raw physical source to dbt
source, dbt source to dbt staging, dbt staging to materialized PostgreSQL
staging, and materialized staging to the dbt mart. DataHub's native dbt
ingestion may additionally emit the explicitly allowlisted raw physical source
to materialized staging edge; no other shortcut or star topology is accepted.
Missing or mismatched ingestion lineage causes verification to fail.

The default GMS URL inside the optional runner is
`http://host.docker.internal:18080`. Linux users can rely on the Compose
`host-gateway` mapping or set `DATAHUB_GMS_URL_CONTAINER` explicitly.
