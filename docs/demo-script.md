# 2:50 demo script

## 0:00–0:12 — The risk

Show the healthy executive revenue result.

> Wrong data is more expensive than no data. DataRescue proves a repair before it ships.

## 0:12–0:28 — Metadata is evidence

Open the case and point to the DataHub lineage, business glossary, freshness, and owner. The definition of recognized revenue explicitly excludes processing fees.

## 0:28–0:43 — Trigger real drift

Apply the fixture that replaces `amount` with `gross_amount` and `net_amount`. Show the schema event entering the queue once.

## 0:43–1:08 — Gather context

Show the lineage path and evidence references. Explain that the model may propose candidates, but deterministic gates own the decision.

## 1:08–1:38 — Reject the convincing wrong fix

The `gross_amount` candidate compiles and its primary keys match, but it conflicts with the glossary and increases revenue by 3.40%. DataRescue rejects it.

The `net_amount` candidate matches the glossary, has 0.00% total variance, 100% PK overlap, and passes all eight dbt tests.

## 1:38–2:00 — Prove the patch

Show the isolated candidate schema, SQL diff, and validation ledger. Every claim links to an artifact or DataHub URN.

## 2:00–2:20 — Real work, bounded autonomy

Open the real draft GitHub PR. Emphasize: human merge is required and the DataHub incident remains active.

## 2:20–2:35 — Close the loop

After the human merge, run post-deploy verification. Only then show `RECOVERED`, the resolved incident, and cleared degraded tag.

## 2:35–2:45 — Fail closed

Switch to the unsafe fixture. No candidate passes. Show `AUTO-REPAIR REFUSED` and run the guarded downstream command; it exits with code 75.

## 2:45–2:50 — Close

> DataRescue. Prove the fix before it ships.
