# Test-coverage gate & ramp

Coverage is enforced as a CI gate that *fails the build* below a
ratcheted floor. This doc is the operator + contributor guide for that
gate: where it lives, why it's a ramp instead of a single hard
threshold, where we are on the ramp, and how to advance it.

The policy itself is recorded as [ADR-0010](adr/0010-coverage-ramp-policy.md).

## TL;DR

| Thing | Value | Source of truth |
|---|---|---|
| Packages measured | `engine`, `nexus_sdk` | `pyproject.toml` → `[tool.pytest.ini_options] addopts` |
| Measured baseline | 92.63 % | `pyproject.toml` → `[tool.coverage.report]` comment |
| Ramp schedule | 85 → 88 → 90 → 92 → 93 | same comment |
| **Current floor** | **85 % (phase 1 of 5)** | `pyproject.toml` → `fail_under` |
| Reports | stdout (`term-missing`) + `htmlcov/index.html` | `addopts` |

The floor is **above zero and below the measured baseline on purpose**
— see "Why a ramp" below.

## Where the gate lives

There are two knobs and they must stay in sync:

1. **`pyproject.toml` → `[tool.coverage.report] fail_under`** — this is
   the authoritative floor. `coverage.py` reads it whenever coverage is
   collected, so a plain `pytest` (no extra flags) enforces it.

   ```toml
   [tool.coverage.report]
   show_missing = true
   # Coverage gate — see docs/coverage-ramp.md for the full ramp schedule.
   # Baseline (measured): 92.63%. Ramp: 85 -> 88 -> 90 -> 92 -> 93.
   # Current step: 85 (phase 1 of 5).
   fail_under = 85
   ```

2. **`Makefile` → `test`** passes `--cov-fail-under=85` *explicitly*:

   ```make
   test: ## Run test suite with coverage gate (phase 1 of ramp, see docs/coverage-ramp.md)
   	uv run pytest --cov-fail-under=85
   ```

   This is redundant with the pyproject `fail_under` but makes the gate
   visible to anyone who reads `make help` without opening
   `pyproject.toml`. **If you bump one, bump the other in the same
   commit.**

Coverage is turned on unconditionally via `addopts`, so there is no
"run without coverage" escape hatch short of editing `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-ra --strict-markers --cov=engine --cov=nexus_sdk --cov-report=term-missing --cov-report=html"
```

## How CI applies it

`.github/workflows/ci.yml` runs `python -m pytest tests`. Because
`addopts` injects `--cov` and `[tool.coverage.report] fail_under` is
set, that single command:

1. collects coverage for `engine` + `nexus_sdk`,
2. prints `term-missing` to the job log,
3. writes `htmlcov/`,
4. **exits non-zero if total coverage is below `fail_under`.**

So the gate is on every CI run, not just `make test`. CI also uploads
the coverage report as a build artifact for offline review.

## Why a ramp, not a single threshold

When the gate was introduced the measured coverage was already
**92.63 %**. We did *not* set `fail_under = 93` on day one, even though
the code passed it at that instant, for three reasons:

1. **Don't strand in-flight work.** Freezing at the current maximum
   means the next PR that lands a feature with imperfect coverage is
   red through no fault of its own — it inherits everyone else's
   slack. A floor of 85 gives roughly 7 points of headroom for
   feature work to land while the broader test backlog is chipped at.
2. **Ratchet, don't cliff.** Moving 85 → 88 → 90 → 92 → 93 in steps
   lets each bump be a small, reviewable PR that proves CI is green at
   the new floor *before* committing to it. A one-shot jump to 93 is
   all-or-nothing.
3. **Headroom reflects reality.** The 0.37-point gap between baseline
   (92.63) and ceiling (93) is intentional: 93 is a stretch target, not
   the resting point. The ramp ends there rather than chasing 100 %,
   which on a project with stub routes (see
   [known-limitations.md](known-limitations.md)) would be busywork.

## Current phase

**Phase 1 of 5 — floor at 85 %.**

Measured coverage at this phase is ~93 % (run `coverage report` to
confirm against your checkout). The floor therefore has ~8 points of
headroom, which is the designed-in margin for phase 1.

The phase advances when:

- the measured coverage has been **stably above the next step** for at
  least one release cycle (no one-off spikes), and
- a maintainer opens a PR that bumps `fail_under` (and the Makefile
  `--cov-fail-under`) to the next value, updates the comment, and
  updates the "Current phase" line in this doc.

## Reading a coverage report

```bash
make test                          # prints term-missing + writes htmlcov/
python -m http.server -d htmlcov   # browse htmlcov/index.html
```

Things to know when reading the report:

- **The gate is total coverage across `engine` + `nexus_sdk`.** A
  single uncovered module can sink the total if it's large; check the
  `Missing` column for the file, not just the percentage.
- **Branch coverage is not configured.** We measure line coverage
  only. Adding branch coverage is a deliberate non-goal until the line
  floor is at 92 — adding branch measurement now would move the
  baseline and force a re-baseline of the whole ramp.
- **Partial lines** show as `Nx->exit` in the `Missing` column (a line
  that ran but one of its branches didn't). Those count as covered for
  the gate.
- **`# pragma: no cover`** is allowed but should be rare and carry a
  comment explaining *why* the line is unreachable (e.g. a
  defensive `else` on a validated input). GREP the tree before adding
  one — most "uncoverable" code is actually a missing test.

## Test markers

`pyproject.toml` declares two markers; both are collected by default:

| Marker | Meaning |
|---|---|
| `slow` | Long-running tests. Skip locally with `pytest -m "not slow"`. |
| `integration` | Requires an external service (Postgres, Valkey, a market-data vendor). CI runs these against real services. |

Coverage is collected across all markers in the default run, so
excluding `slow` locally will under-report vs. CI. When reconciling a
local number against the gate, run the full suite.

## Running locally without the gate

You shouldn't need to, but if you want a raw report (e.g. to see what
a WIP branch looks like before writing tests):

```bash
uv run pytest --no-cov                         # skip collection entirely
uv run pytest --cov-fail-under=0               # collect, never fail
```

`--no-cov` is the cleaner option; `--cov-fail-under=0` still pays the
collection cost. Neither is a way to bypass CI — the gate runs from
`pyproject.toml` in CI, not from your shell flags.

## Advancing the ramp (maintainer checklist)

To move from the current phase to the next:

1. Confirm `coverage report` shows total ≥ next step + 1 (keep ≥1 point
   of headroom even after the bump).
2. In `pyproject.toml`, edit the comment block *and* `fail_under`:
   - update `# Current step: <old> (phase N of 5)` → new value / phase,
   - set `fail_under = <new>`.
3. In `Makefile`, update the `test` target's `--cov-fail-under=<new>`
   and the `## ` help comment.
4. Update **"Current phase"** in this doc.
5. Open the PR; CI must be green. If it's red, the bump isn't ready —
   close the PR and write tests instead of loosening the floor.
6. On merge, the new floor is now the gate for *every subsequent PR*.

## Common failures

- **`FAIL Required test coverage of 85.0% not reached. …`** — your PR
  dropped total coverage below the floor. The `term-missing` output
  names the files; add or fix tests there. Do **not** lower
  `fail_under` to make it pass.
- **Green locally, red in CI** — you ran `pytest -m "not slow"` or
  skipped `integration`. CI runs everything. Run the full suite.
- **Numbers differ between `make test` and `pytest`** — they shouldn't;
  both read the same `pyproject.toml`. If they do, you have a stale
  `.coverage` file: `rm -f .coverage` and re-run.

## See also

- [ADR-0010 — Phased test-coverage ramp](adr/0010-coverage-ramp-policy.md)
- [known-limitations.md](known-limitations.md) — the per-file lint
  ignore list that intersects with test/code quality.
- [development.md](development.md) — the `make test` / lint / typecheck
  loop this gate plugs into.

## Per-module ramp (issue #648)

The global `fail_under` above is the **coarse** gate: it stops the
*whole project* dropping below a floor. Issue #648 adds a finer layer
on top — a **per-module ratchet** that records an individual floor for
every source file and bumps each one upward as that file's measured
coverage rises.

| Thing | Value | Source of truth |
|---|---|---|
| Floors config | `config/coverage-floors.json` | checked-in baseline |
| Engine | `scripts/coverage_ramp.py` | `seed` / `bump` / `check` |
| Weekly bump job | `.github/workflows/coverage-ramp.yml` | Mondays 03:17 UTC |
| Local targets | `make coverage-check`, `make coverage-bump` | `Makefile` |

The floors are seeded at **measured − 1 %** (floored), so every module
starts passing with at least one point of headroom. The ratchet is
**monotonic**: a floor can only ever go *up*, never down. The weekly
job re-measures coverage and, for any module whose coverage rose,
opens a PR bumping its floor — the bump is reviewed before it binds.

The per-module `check` runs **weekly** (in the ramp workflow), not on
every PR, deliberately: per-file coverage has more run-to-run variance
than the global total, and wiring it into the per-PR gate before that
variance is characterised would risk flakes on the main gate. Once a
few weekly cycles confirm the floors are stable, `check` can be
promoted into `.github/workflows/ci.yml` as a second gate.

### Operating the ramp locally

```bash
make test                # collect .coverage (enforces the global floor)
make coverage-check      # fail if any module is below its floor
make coverage-bump       # dry-run: show which floors would rise
```

To write the bumps (what the weekly job does in its PR):

```bash
uv run python scripts/coverage_ramp.py \
  --coverage-json .coverage.json \
  --floors config/coverage-floors.json bump --apply
```

The logic (ratchet / check / diff) is unit-tested in
`tests/test_coverage_ramp.py`; the I/O shells are kept thin on
purpose.
