// Smoke test — runs in <1 minute. Verifies the API surface is alive end-to-end.
// Intended to run in CI on every PR that touches engine/api/.
//
// Required env:
//   NEXUS_BASE_URL          e.g. https://staging.example.com
//   NEXUS_LOAD_USER         load-test user email (MFA disabled)
//   NEXUS_LOAD_PASS         load-test user password
//
// Run:
//   k6 run tests/load/api-smoke.js
//
// Reference: docs/operations/load-testing.md

import http from 'k6/http';
import { check, sleep } from 'k6';
import { login, authHeaders } from './lib/auth.js';

export const options = {
  scenarios: {
    smoke: {
      executor: 'constant-vus',
      vus: 1,
      duration: '30s',
    },
  },
  thresholds: {
    // Smoke must be effectively perfect — any failure is a real signal.
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<1000'],
    'http_req_duration{name:health}': ['p(95)<200'],
  },
};

export function setup() {
  const baseUrl = __ENV.NEXUS_BASE_URL;
  if (!baseUrl) {
    throw new Error('NEXUS_BASE_URL is required');
  }
  const { token } = login(baseUrl, __ENV.NEXUS_LOAD_USER, __ENV.NEXUS_LOAD_PASS);
  return { baseUrl, token };
}

export default function (data) {
  // 1. Health check — should be sub-200ms.
  const health = http.get(`${data.baseUrl}/api/v1/health`, {
    tags: { name: 'health' },
  });
  check(health, { 'health 200': (r) => r.status === 200 });

  // 2. Authenticated read — portfolio listing.
  const portfolios = http.get(`${data.baseUrl}/api/v1/portfolio`, {
    headers: authHeaders(data.token),
    tags: { name: 'portfolio_list' },
  });
  check(portfolios, {
    'portfolio 200': (r) => r.status === 200,
    'portfolio is JSON': (r) => {
      try { return Array.isArray(r.json()) || typeof r.json() === 'object'; }
      catch (_) { return false; }
    },
  });

  // 3. Reference data — instruments / exchanges (cheap GET).
  const ref = http.get(`${data.baseUrl}/api/v1/reference/exchanges`, {
    headers: authHeaders(data.token),
    tags: { name: 'reference_exchanges' },
  });
  check(ref, { 'reference 200': (r) => r.status === 200 });

  sleep(1);
}
