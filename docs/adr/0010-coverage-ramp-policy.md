# ADR-0010: Phased test-coverage ramp

- **Status**: Accepted
- **Date**: 2026-06-25
- **Deciders**: Lead maintainer + CI/testability reviewer
- **Tags**: testing, ci, quality-gate

## Context and Problem Statement

The engine and its SDK (`nexus_sdk`) had grown to ~14.9k statements of
Python with no enforced coverage gate. Measured coverage at the time
of this decision was **92.63 %** — healthy, but unenforced: nothing in
`make test` or CI stopped a PR from landing that dropped coverage to
arbitrarily low values, and the only signal was a human noticing the
`term-missing` output.

We needed a coverage floor that (a) is enforced on every CI run, (b)
does not strand in-flight feature work the day it lands, and (c) can
rise over time as the test backlog is chipped away. The question was
*where to set the floor and how to move it*.

The gate mechanism itself (`pytest-cov` + `[tool.coverage.report]
fail_under`) was already a solved problem; the decision here is purely
about **policy**: the starting floor, the schedule, and the rules for
advancing.

## Decision Drivers

- **Block regressions, not features.** A gate that fails on the first
  PR after it ships is a gate that gets reverted. The floor must have
  real headroom below the measured baseline.
- **Monotonic ratchet.** The floor should only ever go up; a gate that
  gets lowered erodes trust and signals the test backlog is winning.
- **Small, reviewable steps.** Each advance should be a PR that
  proves CI is green at the new floor before it binds everyone else.
- **Honest ceiling.** The project has deliberately stubbed routes and
  scaffolded-but-unwired code (see
  [known-limitations.md](../known-limitations.md)). Chasing 100 % would
  mean writing tests for code we intend to throw away. The ceiling
  should be a stretch target grounded in the real baseline, not 100.
- **Single source of truth.** The floor value and the schedule must
  live in one place that both `make test` and CI read, so they cannot
  drift.

## Considered Options

1. **Freeze at the measured baseline (≈93 %) immediately.**
2. **Advisory-only coverage** — print the number, never fail the build.
3. **Phased ramp** — start well below baseline, ratchet up on a fixed
   schedule (85 → 88 → 90 → 92 → 93).

## Decision Outcome

Chosen option: **Option 3 — phased ramp**, because it satisfies all
four drivers without the failure modes of the alternatives. The floor
starts at **85 %** (≈7.5 points below the 92.63 % baseline), giving
feature work headroom to land while the gate is enforced from day one.
The schedule is `85 → 88 → 90 → 92 → 93`; the final step (93 %) is a
stretch target ≈0.37 points above baseline, deliberately not 100 %.

The floor is recorded in `pyproject.toml` under
`[tool.coverage.report] fail_under`, with the baseline and full
schedule in an adjacent comment so the policy is readable next to the
value that enforces it. `make test` mirrors the value via an explicit
`--cov-fail-under` flag so the gate is visible in `make help`; CI
enforces it through the same `pyproject.toml` on every run.

### Three-layer enforcement

The global floor above is the **coarsest** of three layers; issues
#648 and #656 added two finer ratchets on top. All three are enforced
by the same `scripts/coverage_ramp.py` engine; they differ in
granularity and cadence:

| Layer | Granularity | Cadence | Floors file | Gate |
|---|---|---|---|---|
| Global | whole project | every PR | `pyproject.toml` `fail_under` | `pytest` via `addopts` |
| Per-module (dir) (#656) | one per source directory, statement-weighted | every PR | `config/coverage-module-floors.json` | `ci.yml` "Per-module coverage gate" |
| Per-file (#648) | one per source file | weekly | `config/coverage-floors.json` | `.github/workflows/coverage-ramp.yml` |

The split cadence is deliberate. Per-file coverage has high
run-to-run variance (one flaky test or one large file swings a number),
so gating every PR on it would flake the main gate; it therefore runs
weekly, opens a bump PR, and surfaces regressions without blocking
PRs. Per-directory coverage is statement-weighted across a directory's
files, which dilutes that variance, so it **is** safe on every PR and
is the layer wired into `ci.yml`. All ratchets are **monotonic** — a
floor only ever rises — and are seeded at measured − 1 % (floored) so
each starts with at least one point of headroom.

### Consequences

- **Positive** — Every CI run fails below the floor; coverage can no
  longer silently rot. The ratchet gives maintainers a low-friction
  way to tighten quality over time (one PR per step). New contributors
  see a concrete, achievable bar instead of an unenforced number.
- **Negative** — Headroom shrinks at each step, so late-phase PRs that
  add untested code will need proportionally more tests to land. The
  ramp is a maintenance commitment: someone has to drive each
  advance, or the floor stalls at 85 indefinitely. Branch coverage is
  deferred (see [coverage-ramp.md](../coverage-ramp.md)) so the gate
  under-reports logical paths until line coverage is at the ceiling.
  The per-file weekly layer does not block PRs, so a file can regress
  for up to a week before the weekly job flags it — the per-module
  per-PR layer catches coarse regressions faster but not single-file
  ones.
- **Neutral** — The global knobs (`pyproject.toml` `fail_under` and
  the Makefile flag) must be bumped together; this is documented as a
  checklist rather than automated, because a single source of truth
  plus an explicit mirror was judged more readable than a generated
  value. The two floors files are bumped via the same script with a
  `--module-level` switch rather than two scripts, to keep one
  ratchet implementation.

## Pros and Cons of the Options

### Option 1 — Freeze at baseline (≈93 %) immediately

- **Pros:** Maximum enforcement from day one; no "we'll get to it"
  debt.
- **Cons:** Strands every in-flight branch whose coverage is below 93,
  even if the uncovered code isn't theirs. The first PR after landing
  the gate is red, which makes the gate politically fragile and likely
  to be weakened under pressure. No room for legitimate
  scaffold-then-test work.

### Option 2 — Advisory-only coverage

- **Pros:** Zero friction; never blocks a PR.
- **Cons:** Defeats the entire purpose. We already had advisory
  coverage (the `term-missing` print) and it did not prevent drift.
  This option is "do nothing" with a flag on it.

### Option 3 — Phased ramp (chosen)

- **Pros:** Enforced from day one with room to breathe; monotonic
  ratchet matches how test backlogs actually get paid down; each step
  is a small PR with a green-CI proof point; ceiling grounded in the
  real baseline rather than an arbitrary 100 %.
- **Cons:** Requires ongoing maintenance to advance; the gap between
  floor and measured coverage can mask regressions in the headroom
  band (a PR that drops measured 93 → 86 still passes the 85 floor).

## Links

- Policy guide: [`docs/coverage-ramp.md`](../coverage-ramp.md)
- Enforcing config: `pyproject.toml` → `[tool.coverage.report]` and
  `[tool.pytest.ini_options] addopts`; `Makefile` → `test` target.
- Floors baselines: `config/coverage-module-floors.json` (per-PR,
  #656) and `config/coverage-floors.json` (weekly, #648).
- Engine: `scripts/coverage_ramp.py` (`seed` / `bump` / `check`, plus
  `--module-level`); tests in `tests/test_coverage_ramp.py`.
- Related: [ADR-0004](0004-task-queue-taskiq.md) (the worker whose
  tasks this gate keeps testable), [known-limitations.md](../known-limitations.md)
  (the stubbed code that caps the realistic ceiling below 100 %).
