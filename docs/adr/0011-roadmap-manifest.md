# ADR-0011: Roadmap manifest as machine-readable source of truth

- **Status**: Accepted
- **Date**: 2026-06-25
- **Deciders**: Lead maintainer + reliability reviewer
- **Tags**: roadmap, governance, observability, tooling

## Context and Problem Statement

Umbrella issue #166 tracks the 57-issue post-gap-analysis expansion
(#109–#165) across six delivery buckets, six dependency chains, and three
peer initiatives. As written, that tracking lives entirely in GitHub issue
prose: checkboxes inside a markdown body, bucket labels in sub-headers,
dependency chains in prose. That is fine for a human reading the issue, but
it is hostile to automation:

* There is no single place a dashboard, a release checklist, or a "what do
  I pick up next?" command can query for *structure* (which issues belong
  to which bucket, what depends on what) without scraping prose.
* Status (open/closed) and structure (which issues exist, how they group)
  are entangled in the same markdown, so a checkbox tick and a bucket
  rename look identical to a scraper.
* Nothing catches a typo that deletes an issue from the roadmap or renames
  a `bucket:*` label out from under automation that filters on it.

We needed a representation that keeps the **structural truth** (the 57
issues, the buckets, the chains, the exit criteria) stable and
machine-checked, while leaving **status** to flow in from GitHub at query
time.

## Decision

Add a versioned YAML manifest — `engine/roadmap/roadmap.yaml` — as the
canonical structural source of truth, plus a `engine.roadmap` module that
parses, **structurally validates**, and analyzes it.

Two concerns are kept strictly separate:

1. **Structure** lives in the manifest and is validated at load time by
   `load_roadmap()`. Validation is exhaustive and aggregates every problem
   in one pass:
   - exactly 57 issues forming the contiguous range #109–#165,
   - no duplicate issue numbers,
   - every issue belongs to exactly one bucket (and its recorded bucket
     matches the bucket that lists it),
   - unique bucket labels and chain slugs,
   - every dependency-chain reference resolves and chains have no repeats,
   - the `tracking_issue` matches the baked-in `TRACKING_ISSUE` (166).

   A manifest that fails any check raises `RoadmapValidationError` so a
   bad edit never ships silently.

2. **Status** (open/closed) is supplied at runtime via
   `Roadmap.with_statuses({number: bool})`, never stored in the manifest.
   Analysis helpers — `completion()`, `is_bucket_exited()`,
   `chain_head()`, `chain_blocked()`, `actionable_issues()` — consume it
   and return immutable stats/issues.

A `to_markdown()` method renders a status report shaped like the umbrella
issue body, and `scripts/roadmap_status.py` exposes it on the CLI with an
optional `--statuses` JSON merge and a `--check` gate ("all buckets
exited"). This is the intended wiring point for a future status-sync job
that fetches issue state from GitHub and diffs the regenerated report
against issue #166 to detect checkbox drift.

## Consequences

**Positive**

* Renaming a `bucket:*` label or dropping an issue is now a loud CI/test
  failure (`tests/test_roadmap.py` pins the bucket order and the exact
  #109–#165 set), not silent drift.
* Dashboards, release checklists, and "what's next" tooling have a typed,
  dependency-respecting API (`chain_head`, `actionable_issues`) instead of
  scraping prose.
* The manifest changes rarely (only when the roadmap itself changes);
  status queries are always fresh because status is never persisted here.

**Negative**

* The manifest can drift from the umbrella issue body if someone edits the
  issue without updating the YAML. This is mitigated by the structural
  tests and by the status report's mirroring the issue body, but it is a
  human-discipline check, not an enforced one — until a status-sync bot
  exists.
* The baked-in `TRACKING_ISSUE` / `EXPECTED_ISSUE_RANGE` constants mean a
  renumbered umbrella or a genuinely different-sized roadmap is a code
  change, not a data change. That is intentional: those are rare, loud
  events.

## Alternatives considered

* **Scrape the issue body.** Rejected: prose scraping is brittle, cannot
  distinguish status from structure, and gives no way to validate.
* **Query the GitHub API directly in the module.** Rejected: it would add
  a hard network/credentials dependency to import-time logic. The module
  instead accepts a status map; the API call is a separate concern for the
  CLI/sync job.
* **Store status in the manifest too.** Rejected: it would make the
  manifest churn on every issue close and invite stale-state bugs.
