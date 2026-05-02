// Reusable auth helpers for k6 load tests.
//
// Usage:
//   import { login, authHeaders } from './lib/auth.js';
//
//   export function setup() {
//     const { token } = login(__ENV.NEXUS_BASE_URL, __ENV.NEXUS_LOAD_USER, __ENV.NEXUS_LOAD_PASS);
//     return { token };
//   }
//
//   export default function (data) {
//     const res = http.get(`${__ENV.NEXUS_BASE_URL}/api/v1/portfolio`, {
//       headers: authHeaders(data.token),
//     });
//     check(res, { '200': (r) => r.status === 200 });
//   }

import http from 'k6/http';
import { check, fail } from 'k6';

/**
 * Acquire an auth token. Throws if the login fails.
 *
 * @param {string} baseUrl   - e.g. "https://staging.example.com"
 * @param {string} username  - load-test user (must NOT have MFA enabled)
 * @param {string} password  - load-test password
 * @returns {{ token: string }}
 */
export function login(baseUrl, username, password) {
  const res = http.post(
    `${baseUrl}/api/v1/auth/login`,
    JSON.stringify({ email: username, password }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'login' } },
  );

  const ok = check(res, {
    'login returned 200': (r) => r.status === 200,
    'login returned a token': (r) => {
      try { return !!r.json('access_token'); } catch (_) { return false; }
    },
  });

  if (!ok) {
    fail(`login failed: status=${res.status} body=${(res.body || '').slice(0, 200)}`);
  }

  return { token: res.json('access_token') };
}

/**
 * Build the Authorization headers for an authenticated request.
 *
 * @param {string} token
 * @param {Record<string, string>} [extra]
 * @returns {Record<string, string>}
 */
export function authHeaders(token, extra = {}) {
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    ...extra,
  };
}
