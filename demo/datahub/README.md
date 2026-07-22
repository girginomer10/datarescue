# DataHub integration recipes

DataRescue does not embed an unofficial, brittle clone of DataHub's full stack
in this Compose file. Run DataHub Core with its supported quickstart or point at
an existing GMS, then use the optional ingestion runners:

```bash
datahub docker quickstart
make datahub-check
make datahub-ingest
```

Or run the complete connected path in one command:

```bash
make demo-connected
```

This installs the pinned DataHub CLI, starts the official v1.6.0 quickstart,
generates and ingests both metadata sources, and launches the app with
`DATARESCUE_REPLAY_MODE=false` and `DATARESCUE_EXECUTION_MODE=postgres`.
DataHub's quickstart itself requires Docker Compose v2.

The command is deliberately fail-fast. Before starting the heavy stack it
requires a configured live MCP endpoint, OpenAI key, authenticated `gh` CLI,
and an accessible repository. An environment token is optional when `gh` can
use its credential store. After baseline ingestion it also calls the MCP
context tool for the monitored URN; a non-working endpoint stops the run.

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
→ live MCP check
→ API healthy in postgres mode
→ Actions consumer subscribed
→ apply database drift
→ ingest changed schema / emit MCL
```

The official pinned Quickstart publishes Kafka and Schema Registry on
`127.0.0.1:9092` and `http://127.0.0.1:8081`. DataRescue uses those explicit
defaults; it does not inspect or guess Docker networks/container names. For an
external or remapped DataHub installation, set both endpoints explicitly:

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
| Platform instance | `datarescue-demo` | `target_platform_instance: datarescue-demo` |
| Platform | `postgres` | `target_platform: postgres` |
| Database in name | Native PostgreSQL name | `include_database_name: true` |

For the monitored fixture, both sources resolve to:

```text
urn:li:dataset:(urn:li:dataPlatform:postgres,datarescue.raw.payments_raw,PROD)
```

The dbt recipe requires `manifest.json`, `catalog.json`, and `run_results.json`.
`make dbt-artifacts` generates all three before ingestion.
The recipe reads their directory from `DBT_ARTIFACT_ROOT`; the container runner
sets it to `/workspace/demo/dbt/target` automatically.

The default GMS URL inside the optional runner is
`http://host.docker.internal:8080`. Linux users can rely on the Compose
`host-gateway` mapping or set `DATAHUB_GMS_URL_CONTAINER` explicitly.
