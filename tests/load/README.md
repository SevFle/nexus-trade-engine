# Load tests (k6)

[k6](https://k6.io/) load + performance scripts targeting the engine's
HTTP surface.

| Script              | Purpose                                                | Duration | Triggered by                                 |
|---------------------|--------------------------------------------------------|----------|----------------------------------------------|
| `api-smoke.js`      | 1 VU, 30 s — alive end-to-end, every key route 2xx.    | ~30 s    | Manual; recommended on every staging deploy. |
| `api-baseline.js`   | Constant-arrival-rate baseline at `NEXUS_BASELINE_RPS` (default 20 RPS) for 5 min. | 5 min | Weekly cron + manual. |

## Running locally

```bash
# Install k6 (https://grafana.com/docs/k6/latest/get-started/installation/).
brew install k6                      # macOS
# or: sudo apt-get install k6         # Linux

# Set the target.
export NEXUS_BASE_URL=https://staging.example.com
export NEXUS_LOAD_USER=load-test@example.com
export NEXUS_LOAD_PASS='use-a-real-but-non-prod-password'

k6 run tests/load/api-smoke.js
k6 run tests/load/api-baseline.js
```

## Running in CI

`.github/workflows/load-test.yml` runs the suite weekly and on manual
trigger. Required repo secrets:

- `NEXUS_BASE_URL`
- `NEXUS_LOAD_USER`
- `NEXUS_LOAD_PASS`

The workflow uploads `load-test-summary.json` as an artifact (30-day
retention) so regressions can be diffed.

## Conventions

- **Tag every request** via `tags: { name: '<route>' }`. Thresholds
  reference these names.
- **Use the helpers in `lib/auth.js`** for login / Authorization
  headers. Don't re-implement the login flow per script.
- **No destructive writes.** The baseline writes via the async-
  backtest path (returns 202; worker may or may not run it). Don't
  add scripts that mutate prod-shaped data without a separate
  destructive-OK env flag.
- **No MFA-enabled users.** The load-test user must be a flat
  email + password. MFA challenge flows are not part of the load
  surface today (and should never be).

## Adding a new scenario

1. Drop the script in this directory.
2. Tag every request.
3. Add thresholds for the routes you exercise — start strict, relax
   if real measurements justify it.
4. Add a row to the table at the top of this README.
5. Add the scenario name to the `workflow_dispatch.inputs.scenario`
   choice list and the `case` statement in
   `.github/workflows/load-test.yml`.

## Out of scope (for now)

- **WebSocket traffic.** Will be added once the WebSocket surface
  (#7) lands; k6 supports `k6/ws` natively.
- **MCP server load.** Will be added once the MCP server (#104–#106)
  lands; likely a separate `mcp-baseline.js` using the MCP transport.
- **Spike + stress profiles.** Tracked as a follow-up — the current
  scripts give us regression signal; spike/stress are useful once
  capacity planning becomes a recurring need.

## Related

- Operator runbook: [`docs/operations/load-testing.md`](../../docs/operations/load-testing.md)
- SLOs the baseline thresholds anchor against:
  [`docs/operations/slos.md`](../../docs/operations/slos.md)
