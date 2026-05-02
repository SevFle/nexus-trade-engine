# Load testing

We run k6-based load tests against a **staging** environment
weekly + on demand. The goal is regression detection: we want to know
if a merged change made the API meaningfully slower or less reliable
under realistic traffic.

The scripts live in [`tests/load/`](../../tests/load/). The CI
workflow lives in
[`.github/workflows/load-test.yml`](../../.github/workflows/load-test.yml).

## Environments

| Environment | Allowed?          | Notes                                                       |
|-------------|-------------------|-------------------------------------------------------------|
| Staging     | Yes — primary use | Mirror of production with synthetic data. Run anytime.      |
| Production  | **No**            | The async-backtest write path the baseline exercises mutates real DB rows. |
| Local dev   | Yes — manual only | Useful when iterating on the script itself.                 |

If you must measure something on production, use read-only smoke
checks (`http_req_failed` + `http_req_duration` on the `/health` and
`/portfolio` endpoints only) and treat any meaningful traffic as an
incident.

## Triggering a run

### From GitHub (recommended)

1. **Actions → Load test → Run workflow**.
2. Pick the scenario (`smoke` or `baseline`) and optionally override
   `NEXUS_BASE_URL` for a one-off run.
3. The workflow uploads `load-test-summary.json` as an artifact —
   download it from the run page when the job finishes.

### From your laptop

```bash
brew install k6   # or: sudo apt-get install k6
export NEXUS_BASE_URL=https://staging.example.com
export NEXUS_LOAD_USER=load-test@example.com
export NEXUS_LOAD_PASS='non-prod-password'

k6 run tests/load/api-smoke.js
k6 run tests/load/api-baseline.js
```

## Reading the output

k6 prints a summary at the end of every run:

```
checks.........................: 99.95%  ✓ 5994  ✗ 3
http_req_duration..............: avg=230ms p(95)=890ms p(99)=1.4s
http_req_failed................: 0.05%   ✓ 6000  ✗ 3
```

The lines that matter:

- **`checks`** — your `check(...)` assertions inside the script. Below
  100% means the API returned an unexpected status or shape.
- **`http_req_duration`** — request latency. The thresholds in the
  scripts (e.g. `p(95)<800` for `portfolio_list`) cause the run to
  exit non-zero if exceeded.
- **`http_req_failed`** — fraction of requests that did not return
  2xx/3xx.

The summary also lists per-tag breakdowns
(`http_req_duration{name:portfolio_list}`) — that's where regressions
usually show up first.

## Linking to SLOs

The baseline thresholds are deliberately a bit tighter than the SLOs
in [`slos.md`](slos.md): we want this test to fail before the SLO
budget is consumed, not after.

| Threshold (baseline)                              | Related SLO                  |
|---------------------------------------------------|------------------------------|
| `http_req_failed < 0.5%`                          | API availability (99.5%)     |
| `http_req_duration{portfolio_list} p(95) < 800ms` | API latency (99% < 1.0s)     |
| `http_req_duration{backtest_submit} p(95) < 1.5s` | Backtest submit (99%)        |

If you tighten or relax a threshold in the scripts, update this table
in the same PR.

## Triaging a regression

1. **Reproduce locally** if possible. Re-run the script against the
   same staging URL — flake vs. real regression.
2. **Diff the artifact.** Download `load-test-summary.json` from the
   failing run and the previous green run; compare the per-tag
   percentiles.
3. **Check recent merges.** A latency regression usually correlates
   with a specific merge — `git log main --since=$(date -d '7 days
   ago' +%Y-%m-%d)` is your friend.
4. **Open an issue** tagged `priority-high` with the diff and a
   pointer to the suspected commit. Don't tighten the threshold to
   make the test pass — that's how we got there.

## Common pitfalls

- **Login fails** — the load-test user has MFA enabled. The scripts
  intentionally hit the flat `/login` path; an MFA-enabled user will
  get an `mfa_required: true` response. Disable MFA on the test user.
- **Backtest endpoint returns 422** — the Pydantic model changed.
  Update `BACKTEST_BODY` in `tests/load/api-baseline.js` to match.
- **`Frontend Lint` fails on the JS** — lint scope intentionally
  doesn't include `tests/load/`. If you see this, check the lint
  config wasn't accidentally widened.
- **CI run hangs** — the runner could not reach the staging URL
  (firewall, expired DNS). Cancel and check the staging environment
  before re-running.

## Out of scope

The k6 surface deliberately does not cover:

- Authentication flows that involve MFA.
- WebSocket traffic (waiting on #7).
- MCP server traffic (waiting on #104–#106).
- Spike / stress profiles for capacity planning. Add when needed.

## Related

- [`tests/load/README.md`](../../tests/load/README.md) — script-level
  conventions.
- [`docs/operations/slos.md`](slos.md) — SLOs the thresholds anchor
  against.
- [`docs/operations/runbooks/api-availability.md`](runbooks/api-availability.md) — what to do when the SLO does break.
