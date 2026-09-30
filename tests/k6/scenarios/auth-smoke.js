/**
 * Auth Smoke Test
 *
 * Verifies auth endpoints are wired up and respond with expected status codes.
 * Does not test end-to-end auth flow (integration tests cover that).
 * 2 VUs, 30 seconds.
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders, buildApiHeaders, buildFormHeaders, randomAlias, isOk, isRedirect } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  vus: 2,
  duration: '2m',
  thresholds: THRESHOLDS.smoke,
};

export default function () {
  const endpoint = randomIntBetween(1, 7);

  switch (endpoint) {
    case 1: {
      // POST /auth/login with fake credentials — expect 401
      const res = http.post(
        `${BASE_URL}/auth/login`,
        JSON.stringify({
          email: 'nonexistent@k6test.invalid',
          password: 'WrongPassword123!',
        }),
        { headers: buildApiHeaders() },
      );
      check(res, {
        'POST /auth/login: status 401': (r) => r.status === 401,
      });
      break;
    }

    case 2: {
      // POST /auth/register — expect 201 or 409
      const uniqueEmail = `k6test+${Date.now()}${randomIntBetween(1000, 9999)}@test.invalid`;
      const res = http.post(
        `${BASE_URL}/auth/register`,
        JSON.stringify({
          email: uniqueEmail,
          password: 'K6TestPass123!',
          name: 'K6 Test User',
        }),
        { headers: buildApiHeaders() },
      );
      check(res, {
        'POST /auth/register: status 201 or 409': (r) => r.status === 201 || r.status === 409,
      });
      break;
    }

    case 3: {
      // POST /auth/logout — expect 200
      const res = http.post(
        `${BASE_URL}/auth/logout`,
        null,
        { headers: buildApiHeaders() },
      );
      check(res, {
        'POST /auth/logout: status 200': (r) => r.status === 200,
      });
      break;
    }

    case 4: {
      // GET /auth/me without auth — expect 401
      const res = http.get(`${BASE_URL}/auth/me`, {
        headers: buildApiHeaders(),
      });
      check(res, {
        'GET /auth/me: status 401': (r) => r.status === 401,
      });
      break;
    }

    case 5: {
      // POST /auth/request-password-reset — always returns 200 (timing-safe)
      const res = http.post(
        `${BASE_URL}/auth/request-password-reset`,
        JSON.stringify({ email: 'nobody@k6test.invalid' }),
        { headers: buildApiHeaders() },
      );
      check(res, {
        'POST /auth/request-password-reset: status 200': (r) => r.status === 200,
      });
      break;
    }

    case 6: {
      // GET /login — expect 302 redirect to /
      const res = http.get(`${BASE_URL}/login`, {
        redirects: 0,
      });
      check(res, {
        'GET /login: status 302': (r) => r.status === 302,
        'GET /login: redirects to /': (r) => {
          const loc = r.headers['Location'] || '';
          return loc === '/' || loc.endsWith('/');
        },
      });
      break;
    }

    case 7: {
      // GET /register — expect 302 redirect to /
      const res = http.get(`${BASE_URL}/register`, {
        redirects: 0,
      });
      check(res, {
        'GET /register: status 302': (r) => r.status === 302,
        'GET /register: redirects to /': (r) => {
          const loc = r.headers['Location'] || '';
          return loc === '/' || loc.endsWith('/');
        },
      });
      break;
    }
  }

  sleep(randomIntBetween(1, 3));
}
