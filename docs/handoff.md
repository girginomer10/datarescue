# Agent handoff

## 2026-07-22 - Codex

- Task: Built the DataRescue hackathon product from an empty repository through the evidence-gated vertical slice.
- Changed: FastAPI case engine and guard, deterministic policy and evidence packages, PostgreSQL/dbt fixtures, DataHub MCL/MCP/GraphQL adapters, OpenAI structured candidate generation, git/draft-PR integration, React Forensic Console, replay evidence, CI, Pages workflow, and submission docs.
- Verified: 23 Python tests, 13 web tests, replay manifest hashes, Python lint/types, production and Pages builds, DataHub Actions recipe validation, and live PostgreSQL/dbt safe plus fail-closed workflows. Re-run the final gate before publishing because this note was written immediately before release.
- Memory: None; no repo semantic-memory system is installed. This chronological note is the handoff source.
- Next: Publish the public repository and use the app to open the real draft PR. Do not merge it. Full DataHub Core/Kafka/MCP/GraphQL execution remains a connected-environment gate and must be shown as `NOT_RUN` until its services and credentials are present.

## 2026-07-22 - Codex (release follow-up)

- Task: Fixed the first GitHub-hosted CI bootstrap failure after publishing `main`.
- Changed: Pinned `astral-sh/setup-uv` to the real `v9.0.0` release because the upstream repository does not publish a floating `v9` ref.
- Verified: GitHub reported the missing `v9` ref in both CI jobs; the `v9.0.0` release and action definition were checked through the GitHub API. Await the replacement CI run before treating the release as green.
- Memory: None; the exact-action-tag requirement is recorded here as a durable release gotcha.
- Next: Push the CI fix, wait for CI and Pages, then create and verify the application-generated draft PR without merging it.

## 2026-07-22 - Codex (published proof)

- Task: Completed public release and exercised the real GitHub draft-PR path through the running DataRescue application.
- Changed: Published `girginomer10/datarescue`, enabled GitHub Pages with Actions, and added the hosted replay plus draft-PR evidence links to the README. The application created branch `datarescue/dr-996c48f0` and draft PR #1 with only the allowlisted dbt model change.
- Verified: `main` CI and Pages passed; the hosted replay returned HTTP 200 and rendered `RECORDED_REPLAY EVIDENCE`; PR #1 is open/draft, changes one file, and passed `quality` plus `postgres-dbt-proof`. Case `DR-996C48F0` remained `PR_OPEN` with an active local incident after PR creation.
- Memory: None; release evidence is captured in this handoff and the external GitHub checks.
- Next: Do not merge PR #1 automatically. A human merge must be followed by `verify-deployment`. Full DataHub Core/Kafka/MCP/GraphQL execution is still `NOT_RUN` because that connected stack and its credentials were unavailable on this machine.

## 2026-07-22 - Codex (persistent completion checkpoint)

- Task: Persisted the exact remaining definition of done so a later `devam et` resumes from the live DataHub gap instead of treating the MVP as complete.
- Changed: Added repo-scoped semantic-memory sources, offline SQLite indexing/search/doctor tooling, future-agent instructions, and `memory/decisions/datarescue-hackathon-definition-of-done.md`.
- Verified: Memory source security scan, incremental index, completion-query retrieval, stale-reference doctor, Python lint, and repository diff checks.
- Memory: The active completion note is the authoritative resume order; the local vector cache is rebuildable and remains outside Git.
- Next: Start with Docker Compose v2 plus the real DataHub MCL-to-incident path, then live MCP/GraphQL/OpenAI, merge-SHA closure, connected containment, fresh-clone demo, genuine replay, video, and Devpost.

## 2026-07-22 - Codex (fresh-run PostgreSQL readiness fix)

- Task: Fixed the GitHub-hosted dbt proof race exposed by the memory-checkpoint push.
- Changed: `scripts/demo-runtime.sh` now waits past PostgreSQL's temporary entrypoint initialization server and proves the initialized audit fixture exists before declaring readiness. Added a durable bugfix memory note.
- Verified: Shell syntax, Ruff, local `make test-demo`, memory sync, and memory doctor passed. Fresh GitHub run `29929649811` then passed both `quality` and `postgres-dbt-proof`; Pages run `29929650837` also passed.
- Memory: Added `memory/bugfixes/postgres-entrypoint-readiness-race.md`; do not replace final-server readiness with a single `pg_isready` probe.
- Next: Resume the completion roadmap at the real DataHub MCL-to-incident path; do not expand UI scope first.

## 2026-07-22 - Automated L6 quality and hardening pass

- Task: Autonomous quality pass over the published MVP. Did not touch the
  connected/credential-dependent gates (real DataHub/OpenAI/GitHub); focused on
  the "definition of done" item 10 — robustness, security, honesty, coverage.
- Verified baseline first: 23 Python tests, 13 web tests, web build, replay
  hash verification, ruff, mypy --strict, memory reindex/doctor, and the live
  PostgreSQL/dbt workflow proof (gross +3.40% rejected, net 0.00% selected)
  all green in this environment.
- Ran an adversarial multi-dimension audit (correctness, security, edge cases,
  frontend, test-coverage, doc-drift) and independently reproduced each fix.
- Changed (all with regression tests; suite grew 23 → 119 Python + 14 web):
  - Serialized ingest, demo reset, and verify_deployment under the single
    worker lock; before this a reset could interleave mid-ingest and split a
    case's event stream across dedup scopes, permanently 500-ing list/get.
  - store.append resolves an (event_id, reset_scope) collision to a
    DuplicateEventError instead of a raw sqlite3.IntegrityError 500.
  - MCL intake: non-object payloads and out-of-range timestamps fail gracefully;
    an out-of-allowlist asset is SKIPPED, not a 500; missing case id serializes
    as null.
  - Executor: delete stale run_results.json before dbt; bound every psycopg
    connection with connect + statement timeouts.
  - GitHub: clean up the local branch + self-heal an orphaned worktree so
    retries work; stop leaking resolved paths in the persisted failure message.
  - SSE honors Last-Event-ID on reconnect and bounds a follow connection.
  - Aligned validate_candidate_sql's alias rule with render_candidate_sql.
  - Web: labeled-region a11y (SectionHeading titleId), lineage tab panels stay
    mounted, coupled lineage node/edge fallbacks, timeout NaN guard, poll-error
    resilience, slugified incident-state class.
  - Docs: fixed the shared-recipe platform_instance claim in architecture.md
    and documented the untracked DATARESCUE_ settings in .env.example.
- Also closed the highest-severity finding defensively: post-deploy
  verification now refuses (409) to resolve a *live* (SUCCEEDED) DataHub
  incident from caller-supplied metrics; a live incident can only be resolved
  in postgres mode, which recomputes evidence and verifies the merge SHA. This
  changes no supported flow (demo/replay incidents are NOT_CONFIGURED; the
  connected launcher runs in postgres mode) but removes the forged-recovery
  path that a github-writes-on + replay-execution misconfiguration exposed.
- Deliberately NOT changed (design-level / user-infra-dependent, documented for
  a human decision): a full API authentication layer (the app is a local demo
  behind CORS; production deployment should sit behind an auth gateway);
  replacing the free-text semantic-verdict substring match with a structured
  glossary-term binding (the six other independent gates still block a wrong
  repair, so a fooled semantic gate cannot ship one).
- Memory: added `memory/bugfixes/workflow-writer-serialization.md`.
- Next: the connected vertical slice (real DataHub Core/Kafka/MCP/GraphQL +
  live OpenAI + human merge) remains the outstanding completion gate.

## 2026-07-22 22:05 +03 - Codex (audit follow-up hardening)

- Task: Reproduced the remaining review findings, repaired the confirmed safety and honesty gaps, and prepared the result for `main`.
- Changed: Added explicit live-evidence provenance and PostgreSQL DSN fail-fast; conflict-safe event IDs and collision-safe readable case IDs; bounded SSE cursors; machine-labelled MCL allowlist handling; safe/idempotent GitHub PR retry ownership checks; honest stale/reset UI states; complete policy accessibility labels; a pinned Python 3.11 bootstrap/runtime; and collision-proof PostgreSQL candidate-schema names.
- Verified: 155 Python tests, 19 web tests, frontend production build, Ruff, mypy, diff checks, and the live PostgreSQL/dbt vertical slice (`gross_amount` rejected at `+3.40%`, `net_amount` selected at `0.00%`, `8/8` checks, distinct evidence artifacts, candidate schemas cleaned). Fresh-clone bootstrap and GitHub-hosted checks are the final release gates for this commit.
- Memory: Added `memory/bugfixes/audit-hardening-identity-provenance-and-retries.md` with the durable identity, provenance, identifier-length, toolchain, and GitHub retry invariants.
- Next: Push the verified commit to `origin/main` and wait for CI/Pages. The real DataHub Core/Kafka/MCP/GraphQL plus live OpenAI and human-merge closure remain separate connected-environment completion gates; do not describe the hosted replay as that proof.

## 2026-07-22 23:10 +03 - Codex (final audit closure)

- Task: Closed the independent final audit findings and prepared the hardened local result for a fast-forward release to `main`.
- Changed: Revalidated every newly created or reused PR against the exact allowed-origin branch head, same repository, non-cross-repository status, and draft state; made interrupted `git worktree add` recovery ownership-safe; made verification temporary-directory cleanup non-fatal; and added regression coverage for each race. Preserved the earlier event identity, exact merge-SHA, live evidence, MCL redelivery, PostgreSQL identifier, and stale-UI fixes.
- Verified: Full `make check` passed with 173 Python tests, 19 web tests, Ruff, mypy across 22 source files, two production builds, and the live PostgreSQL/dbt proof (`gross_amount` rejected at `+3.40%`, `net_amount` selected at `0.00%`, `8/8` checks, candidate schemas cleaned). Replay hashes and repo-memory doctor also passed. Fresh-clone bootstrap and GitHub-hosted CI/Pages remain the release gates.
- Memory: Extended `memory/bugfixes/audit-hardening-identity-provenance-and-retries.md` with exact origin/PR-head, detached-worktree, fresh-context, cleanup, and stale-provenance invariants.
- Next: Commit the intended tree, prove it from a `--no-local` fresh clone, fast-forward `main`, push, and wait for CI/Pages. Do not merge draft PR #1. The connected DataHub/OpenAI/human-merge roadmap remains separate.

## 2026-07-22 23:18 +03 - Codex (audit release complete)

- Task: Published the fully audited local result to `origin/main` and closed every release gate for code commit `bac80c6c3b1691aa3bd86dcbd55b98cc426bfc54`.
- Changed: Fast-forwarded `main` without rewriting history. No recovery PR was merged; draft PR #1 remains open and draft on `datarescue/dr-996c48f0`.
- Verified: A `--no-local` clean clone installed Python 3.11, dbt Core 1.9.8, and dbt-postgres 1.9.0, then passed full `make check` (173 Python, 19 web, live PostgreSQL/dbt proof). GitHub CI run `29954473684` and Pages run `29954473422` completed successfully, and the hosted replay returned the DataRescue page over HTTPS.
- Memory: No new note; the existing audit-hardening and definition-of-done notes remain authoritative.
- Next: Resume at the real DataHub Core/Kafka/MCL-to-incident vertical slice, then prove live MCP/GraphQL/OpenAI, connected containment, explicit human merge plus exact-SHA closure, genuine replay regeneration, video, and Devpost. Do not claim the recorded replay is that connected proof.
