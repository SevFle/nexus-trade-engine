# ADR-0013: Legal compliance gate for strategy scoring surfaces

- **Status**: Accepted
- **Date**: 2026-07-21
- **Deciders**: Lead maintainer + legal reviewer
- **Tags**: legal, compliance, scoring, api

## Context and Problem Statement

Strategy scores in Nexus are derived from quantitative factors (see
[`architecture/analytics.md`](../architecture/analytics.md) and the
`nexus_sdk.scoring` module) and land in the `[0, 100]` composite range.
Exposing a score is not just a numerical act — it is a **compliance
act**. Two distinct, real-world requirements forced a single dedicated
mechanism rather than per-route `if strategy in BLOCKED:` checks:

1. **Flagged strategies.** A strategy under review, withdrawn, or
   flagged for a data-licensing or regulatory reason must not have its
   ranking surfaced to users even though the maths still produces a
   number. The number is correct; the *exposure* is the problem.
2. **Hard compliance ceiling.** An operator may need to cap the
   visible composite below the technical `[0, 100]` range — for
   example, a regulator may forbid any single strategy from being
   advertised as better than `85` even if its raw composite is `94`.

Before this ADR, every surface that touched a score (the scoring
API's `run_scoring` / `get_scoring_results`, the backtest summary,
marketplace search/listing) was a place where ad-hoc compliance
filters could accrete — and therefore a place where one of them would
eventually be missed. PR gh#1609 introduced the gate as the
single source of truth; this ADR records *why* it is shaped the way it
is.

The narrow scope is deliberate: per-symbol flagging, time-windowed
holds, per-user overrides, and DB-backed persistence of the
flagged-strategy set are all *out of scope* for v1. The class is
shaped so a future slice can swap the source from env to DB without
touching a caller.

## Decision Drivers

- **Single chokepoint.** Compliance must apply identically at every
  exposure surface; if a third surface forgets the rule, the engine
  is out of compliance. One validator, composed everywhere, is the
  only structurally enforceable shape.
- **Compute-then-gate, not gate-then-compute.** The math still runs
  (so a snapshot row reflects a real evaluation), but what is
  persisted *and* what is returned is the gated view. The persisted
  snapshot and every downstream surface must see the same compliant
  view.
- **Defence-in-depth on the read path.** A snapshot persisted *before*
  a strategy was flagged (or before the cap was tightened) must be
  brought into compliance at exposure time too — a flag applied today
  cannot be defeated by re-reading yesterday's snapshot.
- **Misconfiguration can never take the scoring surface down.** A bad
  `NEXUS_LEGAL_SCORE_MAX_COMPOSITE` value falls back to the no-op
  default and logs a warning; it does not 500 every scoring call.
- **Testability.** The validator must be overridable from FastAPI's
  `app.dependency_overrides` so the routes' tests do not need to flip
  env vars or restart the process.

## Considered Options

1. **Per-route ad-hoc checks.** Each scoring surface reads
   `settings.legal_score_flagged_strategies` and filters inline.
2. **DB-backed flagged-strategy table queried on every request.**
   Operator toggles via admin API; gate reads through ORM.
3. **A stateless `LegalScoreValidator` class built from settings,
   injected as a FastAPI dependency, applied at the compute→expose
   boundary *and* re-applied on the read path (chosen).**

## Decision Outcome

Chosen option: **Option 3**, because it is the only one that
satisfies all four decision drivers without forcing a DB schema
migration or a runtime dep that operators have to babysit. The
implementation lives in
[`engine/legal/scoring_gate.py`](../../engine/legal/scoring_gate.py)
and is composed by every scoring surface.

Shape of the chosen design:

- `LegalScoreValidator(flagged_strategies, max_score=100.0)` — immutable
  configuration, single shared instance per process, safe to reuse
  across requests.
- `from_settings()` classmethod reads
  `NEXUS_LEGAL_SCORE_FLAGGED_STRATEGIES` (CSV) and
  `NEXUS_LEGAL_SCORE_MAX_COMPOSITE` (float). Parse failures fall back
  to the no-op default and log a warning rather than raising. The
  ceiling is defensively clamped to `min(ceiling, 100.0)` so a
  misconfigured operator value above the technical max cannot exceed
  what `SymbolScore` would accept anyway.
- `validate_score(strategy_id, score)` returns a
  `ScoreValidationResult` with `suppressed` / `capped` / `score` /
  `reason`. Decision tree (first match wins):

  1. `score is None` → suppressed, reason `missing_data`. A score
     that was never computed must never be exposed as if it were a
     real (zero) value.
  2. Non-finite (NaN / ±inf) → suppressed, reason `invalid_score`.
     Mirrors the `math.isfinite` guards already used in the backtest
     PnL path.
  3. `strategy_id ∈ flagged_strategies` → suppressed, reason
     `strategy_flagged`. The maths may be fine, but exposure is
     blocked.
  4. `score > max_score` → clamped down, `capped=True`, reason
     `capped_to_legal_max`.
  5. Otherwise pass-through unchanged.

- `validate_result(ScoringResult)` rebuilds the SDK result with
  survivors re-ranked, so the `rank` field on the surviving scores is
  monotonically consistent with what's exposed.

### Where it composes

| Surface | How the gate is applied |
|---|---|
| `POST /api/v1/scoring/{strategy_name}/run` | `validate_result()` called *before* the snapshot is persisted *and* before the response is serialised. `universe_size` in the response and the persisted row is captured from the **pre-gate** result so suppression cannot shrink the recorded universe. |
| `GET  /api/v1/scoring/{strategy_name}/results` | `_gate_score_dicts()` re-applies `validate_score()` to every entry in the JSONB-round-tripped `scores` list. Stored `rank` values are preserved for survivors so historical ordering stays stable. |
| Marketplace search / listing | Future — same `LegalScoreValidator` will compose once the marketplace surfaces score-sorting. |

The injection point is `get_score_validator()`, a FastAPI dependency.
Tests override it via `app.dependency_overrides[get_score_validator]`
with a controlled `LegalScoreValidator` rather than mutating env.

### Consequences

- **Positive** — one well-tested class is the source of truth; a new
  scoring surface cannot accidentally skip the gate if its author
  follows the existing pattern of depending on `get_score_validator`.
- **Positive** — defence-in-depth on the read path: snapshots stored
  before a flag are brought into compliance on read.
- **Positive** — misconfiguration degrades to a no-op rather than
  outage.
- **Positive** — `universe_size` semantics stay honest: the recorded
  count is "how many symbols were scored", not "how many survived
  the gate".
- **Negative** — the flagged-strategy set is process-local config (CSV
  from env), so updating it is a deploy, not an admin-API call.
  Multi-replica deploys roll the new set in over the rollout window
  rather than atomically.
- **Negative** — per-symbol flagging is not supported; a flagged
  strategy's score is suppressed for *every* symbol it scored.
- **Neutral** — the validator's `from_settings()` is called lazily at
  `get_default_score_validator()` first use; an operator who changes
  `NEXUS_LEGAL_SCORE_FLAGGED_STRATEGIES` after boot still has to
  restart the process for the change to take effect (acceptable for
  v1; see Risks).

## Pros and Cons of the Options

### Option 1 — Per-route ad-hoc checks

- **Pros:** No new class, no new dependency. Smallest initial diff.
- **Cons:** The compliance rule is structurally unenforceable. Every
  new scoring surface is a place to forget the check; an auditor
  cannot grep for one symbol to confirm every surface is in
  compliance. Fails the single-chokepoint driver.

### Option 2 — DB-backed flagged-strategy table

- **Pros:** Operator can toggle a flag at runtime via admin API.
  Atomic across replicas.
- **Cons:** Forces a migration + admin UI in the same PR as the
  compliance rule, which delays the rule itself. A DB outage would
  degrade the scoring surface. The flagged-strategy set is small and
  changes rarely — a full DB round-trip per scoring call is
  over-engineered for v1. The chosen class is shaped so this option
  can land later behind the same interface.

### Option 3 — Stateless settings-driven validator injected as a dependency

- **Pros:** Single chokepoint, composable, testable via
  `dependency_overrides`, degrades safely on misconfiguration, works
  identically on the read path. No migration. The validator is the
  only thing a new scoring surface has to depend on.
- **Cons:** Flagged-strategy set is process-local config, not
  runtime-tunable. Accepted for v1; the class boundary is preserved
  so Option 2 can land behind it without a caller change.

## Links

- Implementation: [`engine/legal/scoring_gate.py`](../../engine/legal/scoring_gate.py)
- Composed by: [`engine/api/routes/scoring.py`](../../engine/api/routes/scoring.py)
- Config knobs: `NEXUS_LEGAL_SCORE_FLAGGED_STRATEGIES`,
  `NEXUS_LEGAL_SCORE_MAX_COMPOSITE` in [`engine/config.py`](../../engine/config.py)
- Surface behaviour: [`api-reference/routes.md` § Scoring — legal gate](../api-reference/routes.md#scoring-legal-gate)
- Env vars: [`deployment.md`](../deployment.md)
- Related PR: gh#1609 (feat(scoring): add legal score validation gate)
