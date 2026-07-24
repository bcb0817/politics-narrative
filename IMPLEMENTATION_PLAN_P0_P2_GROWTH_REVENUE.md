# P0–P2 Growth / Revenue implementation plan

Date: 2026-07-25 JST

## Baseline

- The latest local implementation is authoritative.
- Monitoring remains hourly from 05:00 through 23:00 JST.
- Automated posting remains capped at 8 normal plus 2 breaking posts.
- All X write prohibitions and the existing quality, safety, and budget gates remain unchanged.
- SQLite is the reporting source of truth; JSON remains audit/fallback storage.
- Production X posting and production-task restart are outside this change.

## Audit findings

### P0

- xAI usage already has request IDs, actual/estimated cost separation, tool-call counts,
  a unique index, and one canonical `ticks_to_usd()` conversion.
- `XAI_COST_LEDGER_VERIFIED=false` already enforces a USD 2 effective ceiling.
- A read-only verification command is missing.
- Provider selection is already exclusive in `news.fetch_all_items()`, but provider
  diagnostics, cache-provider metadata, and duplicate-execution audit state are missing.
- `PoliticsNarrativeDailyReview` remains registered and Ready; its action is a functional
  no-op. An administrator-only safe disable script is required.
- `production/update_openai_models.ps1` and `production/migrate_to_openai.ps1` contain
  stale model assumptions and must be quarantined.

### P1

- Quality eval runs are persisted, but the dashboard lacks pass rate, disqualifications,
  previous-run comparison, and 7/30-day trends.
- OpenAI usage has operation-level history, but canonical task types, inference source,
  token totals, and unknown classification are incomplete.
- Engagement candidates and manual statuses exist. Manual post IDs, result collection,
  CSV import, and performance reporting are missing.
- xAI attribution and the minimum sample gate already exist; the existing ROI command
  will be retained and strengthened only where required.

### P2

- Follower snapshots and generic conversion imports exist.
- Per-post follower-window attribution, the complete conversion taxonomy, deduplication,
  conversion dashboards, and digest comparison are missing.
- Weekly review and local extension previews exist, but the requested Shorts/note
  production pipeline, manifest, persistence, and separate weekly budget are missing.

## Implementation order

1. Add idempotent schema extensions and audit/analysis helpers.
2. Add xAI ledger verification and provider diagnostics.
3. Add administrator-safe legacy task script and quarantine stale model scripts.
4. Complete quality/OpenAI/engagement reports.
5. Complete follower, conversion, and digest analysis.
6. Add a local-only weekly content pipeline with strict budget and safety filtering.
7. Add CLI commands, status/budget visibility, fixtures, and regression tests.
8. Run migrations, all tests, compile checks, PowerShell syntax checks, and no-write dry-run.

## Safety controls

- No `.env` replacement.
- No API key or webhook value is printed.
- No X post, reply, quote, repost, like, follow, note publish, or YouTube publish is added.
- Content-pipeline outputs are local drafts only.
- Migrations are additive and repeatable.
- The production task will not be restarted.
