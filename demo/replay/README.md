# Recorded replay evidence

This directory is the immutable, credential-free evidence package used by the
hosted DataRescue demo. It is deliberately **not** presented as a live DataHub,
GitHub, or OpenAI session.

The PostgreSQL totals and dbt summaries were recorded from the public fixture on
2026-07-22. External operations that were not performed are marked `NOT_RUN`.
Context reconstructed from the fixture is marked `RECORDED_REPLAY` and must not
be interpreted as a successful DataHub MCP call.

## Contents

- `artifacts/context-bundle.json`: schema, lineage, ownership, and semantic context.
- `artifacts/reconciliation/*.json`: recorded PostgreSQL candidate comparisons.
- `artifacts/dbt/*.json`: compact summaries of the two isolated dbt builds.
- `artifacts/cases/DR-024.json`: safe-repair case snapshot before any GitHub write.
- `artifacts/evidence/DR-024.json`: deterministic policy ledger and evidence links.
- `artifacts/containment/DR-025.json`: fail-closed case and guard contract.
- `manifest.json`: byte sizes and SHA-256 digests for every JSON artifact.

Verify the package on any platform with Python 3.11+:

```bash
python scripts/verify-replay.py
```

The verifier rejects missing, unexpected, path-traversing, malformed, or
hash-mismatched artifacts. It also checks the demo's core claims: gross revenue
is `+3.40%` and rejected, net revenue is `0.00%` with `100.00%` key overlap and
selected, both dbt builds have `8/8` passing tests, no replay PR claims success,
and containment remains fail-closed with exit code `75`.
