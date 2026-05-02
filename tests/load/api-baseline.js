// Baseline load — sustained traffic at a known RPS for 5 minutes.
// Intended to run weekly on a staging environment to catch regressions
// before they hit production.
//
// Required env:
//   NEXUS_BASE_URL          e.g. https://staging.example.com
//   NEXUS_LOAD_USER         load-test user email (MFA disabled)
//   NEXUS_LOAD_PASS         load-test user password
//
// Optional env:
//   NEXUS_BASELINE_RPS      target requests-per-second (default 20)
//   NEXUS_LOAD_STRATEGY_ID  strategy id used in backtest submissions (default: 'noop')
//
// Run:
//   k6 run tests/load/api-baseline.js
//
// Reference: docs/operations/load-testing.md
//
// What's tested:
//   - GET  /api/v1/portfolio              — read-heavy listing
//   - GET  /api/v1/reference/exchanges    — cached reference read
//   - POST /api/v1/backtest               — async-write path (returns 202)
//
// Why these three:
//   They exercise three distinct backend code paths (DB read, cache read,
//   queue write) without doing anything destructive. The backtest endpoint
//   accepts a no-op payload and returns a row id; the worker may or may not
//   pick it up — that's covered by the task-pipeline SLO, not this test.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { login, authHeaders } from './lib/auth.js';

const BASELINE_RPS = parseInt(__ENV.NEXUS_BASELINE_RPS || '20', 10);

export const options = {
  scenarios: {
    baseline: {
      executor: 'constant-arrival-rate',
      rate: BASELINE_RPS,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: Math.max(20, BASELINE_RPS * 2),
      maxVUs: Math.max(50, BASELINE_RPS * 5),
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.005'],
    'http_req_duration{name:portfolio_list}':       ['p(95)<800',  'p(99)<1500'],
    'http_req_duration{name:reference_exchanges}':  ['p(95)<400',  'p(99)<800'],
    'http_req_duration{name:backtest_submit}':      ['p(95)<1500', 'p(99)<2500'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  const baseUrl = __ENV.NEXUS_BASE_URL;
  if (!baseUrl) {
    throw new Error('NEXUS_BASE_URL is required');
  }
  const { token } = login(baseUrl, __ENV.NEXUS_LOAD_USER, __ENV.NEXUS_LOAD_PASS);
  return { baseUrl, token };
}

const BACKTEST_BODY = JSON.stringify({
  // Minimal accepted payload — operator may need to swap this for whatever
  // the current Pydantic model requires. Kept intentionally small so it does
  // not generate meaningful load on the worker.
  strategy_id: __ENV.NEXUS_LOAD_STRATEGY_ID || 'noop',
  start: '2024-01-01T00:00:00Z',
  end:   '2024-01-02T00:00:00Z',
  symbol: 'AAPL',
});

export default function (data) {
  const r = Math.random();

  if (r < 0.5) {
    const res = http.get(`${data.baseUrl}/api/v1/portfolio`, {
      headers: authHeaders(data.token),
      tags: { name: 'portfolio_list' },
    });
    check(res, { '2xx': (r) => r.status >= 200 && r.status < 300 });
  } else if (r < 0.85) {
    const res = http.get(`${data.baseUrl}/api/v1/reference/exchanges`, {
      headers: authHeaders(data.token),
      tags: { name: 'reference_exchanges' },
    });
    check(res, { '2xx': (r) => r.status >= 200 && r.status < 300 });
  } else {
    const res = http.post(`${data.baseUrl}/api/v1/backtest`, BACKTEST_BODY, {
      headers: authHeaders(data.token),
      tags: { name: 'backtest_submit' },
    });
    // 202 Accepted is the expected success status for the async path.
    check(res, { 'submit accepted': (r) => r.status === 202 || r.status === 201 || r.status === 200 });
  }

  sleep(0.1);
}
