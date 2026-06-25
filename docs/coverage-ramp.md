# Test-coverage gate & ramp

Coverage is enforced as a CI gate that *fails the build* below a
ratcheted floor. This doc is the operator + contributor guide for that
gate: where it lives, why it's a ramp instead of a single hard
threshold, where we are on the ramp, and how to advance it.

The policy itself is recorded as [ADR-0010](adr/0010-coverage-ramp-policy.md).

## TL;DR

The gate is **three layers**, coarsest to finest, each with its own
source of truth and cadence:

| Layer | Granularity | Cadence | Source of truth | Enforced by |
|---|---|---|---|---|
| **1. Global floor** | whole project (`engine` + `nexus_sdk`) | every PR | `pyproject.toml` → `[tool.coverage.report] fail_under` | `pytest` (`addopts` injects `--cov`) |
| **2. Per-module (directory)** | one per source directory, statement-weighted | every PR | `config/coverage-module-floors.json` (issue #656) | `ci.yml` "Per-module coverage gate" step |
| **3. Per-file** | one per source file | weekly | `config/coverage-floors.json` (issue #648) | `.github/workflows/coverage-ramp.yml` |

| Thing | Value | Source of truth |
|---|---|---|
| Packages measured | `engine`, `nexus_sdk` | `pyproject.toml` → `[tool.pytest.ini_options] addopts` |
| Measured baseline | 92.63 % | `pyproject.toml` → `[tool.coverage.report]` comment |
| Ramp schedule (global) | 85 → 88 → 90 → 92 → 93 | same comment |
| **Current global floor** | **85 % (phase 1 of 5)** | `pyproject.toml` → `fail_under` |
| Per-module floors config | `config/coverage-module-floors.json` | per-PR CI gate (#656) |
| Per-file floors config | `config/coverage-floors.json` | weekly ratchet (#648) |
| Ratchet engine | `scripts/coverage_ramp.py` | `seed` / `bump` / `check` (+ `--module-level`) |
| Reports | stdout (`term-missing`) + `htmlcov/index.html` | `addopts` |

The global floor is **above zero and below the measured baseline on
purpose** — see "Why a ramp" below. The per-module and per-file layers
sit on top of it as ratchets that can only ever tighten.

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

## Per-module (directory) ramp (issue #656) — per-PR gate

On top of the global floor sits a **per-directory ratchet**: one floor
per source directory (e.g. `engine/plugins`, `engine/core/oms`),
aggregated statement-weighted across the files in it. Because a single
flaky file is diluted by its siblings, per-directory coverage has much
lower run-to-run variance than per-file coverage, so this is the layer
that is **safe to gate every PR on**. It is wired into
`.github/workflows/ci.yml` as the "Per-module coverage gate" step.

| Thing | Value | Source of truth |
|---|---|---|
| Floors config | `config/coverage-module-floors.json` | checked-in baseline (`level: "module"`) |
| Aggregation | `aggregate_modules` (statement-weighted, by parent dir) | `scripts/coverage_ramp.py` |
| CI gate | `ci.yml` → "Per-module coverage gate" | runs `--module-level check` on every PR |
| Local targets | `make coverage-check-modules`, `make coverage-bump-modules` | `Makefile` |

Floors are seeded at **measured − 1 %** (floored) and the ratchet is
**monotonic** — a floor only ever rises. The per-PR CI step runs
`--module-level check`; raising a floor is a deliberate, reviewed act
(see "Bumping the floors" below), so the floor map in
`config/coverage-module-floors.json` advances more slowly than the
measured coverage and keeps real headroom.

## Per-file ramp (issue #648) — weekly ratchet

The finest layer is a **per-file ratchet**: one floor per source file.
Per-file numbers are noisier (one flaky test or one big file swings
them), so this layer is **not** in the per-PR gate — it runs weekly in
`.github/workflows/coverage-ramp.yml`, which re-measures coverage,
ratchets any floor whose measured coverage rose, and opens a review
PR. It surfaces regressions early even when the per-PR layers still
green; the violation list it prints is the actionable output.

| Thing | Value | Source of truth |
|---|---|---|
| Floors config | `config/coverage-floors.json` | checked-in baseline |
| Weekly bump job | `.github/workflows/coverage-ramp.yml` | Mondays 03:17 UTC |
| Local targets | `make coverage-check`, `make coverage-bump` | `Makefile` |

Both ratchets use the same engine (`scripts/coverage_ramp.py`): the
`--module-level` flag switches it from per-file to per-directory
aggregation and points it at the per-module floors file. The same
`seed` / `bump` / `check` subcommands work for both.

## Operating the ramps locally

```bash
make test                     # collect .coverage (enforces the global floor)
make coverage-check-modules   # FAIL: any directory below its floor (per-PR gate)
make coverage-bump-modules    # dry-run: which directory floors would rise
make coverage-check           # FAIL: any file below its floor (weekly layer)
make coverage-bump            # dry-run: which file floors would rise
```

To write the bumps (what the weekly job does in its PR, per-file):

```bash
uv run python scripts/coverage_ramp.py \
  --coverage-json .coverage.json \
  --floors config/coverage-floors.json bump --apply
```

…or the directory-level equivalent (run manually; CI does not
auto-bump the per-module file):

```bash
uv run python scripts/coverage_ramp.py \
  --coverage-json .coverage.json \
  --module-level bump --apply
```

## Bumping the floors

Neither floors file should be hand-edited value-by-value — re-run the
bump so the history stays monotonic and reviewable:

1. Run `make test` to collect a fresh `.coverage`.
2. Run the relevant `coverage-bump[-modules]` dry-run; review the diff.
3. Re-run with `--apply` (or `APPLY=1`) to write the new floors.
4. Commit `config/coverage-floors.json` and/or
   `config/coverage-module-floors.json` and open a PR. CI must stay
green at the new floors before merge.

For the per-file layer, the weekly `coverage-ramp.yml` workflow does
steps 1–4 automatically and opens the PR; for the per-module layer a
maintainer runs them by hand (it gates every PR, so an auto-bump is
deliberately not wired up).

The selection / ratchet / check / diff logic is unit-tested in
`tests/test_coverage_ramp.py` (including the `aggregate_modules`
roll-up and the `--module-level` CLI path); the I/O shells are kept
thin on purpose.
