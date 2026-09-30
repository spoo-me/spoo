/**
 * Mixed Realistic Traffic Simulation
 *
 * Simulates production-like traffic (~400k req/day) using k6 named scenarios
 * to run different traffic types concurrently with proper weighting.
 *
 * Traffic distribution:
 *   80% redirects     — GET /{short_code}
 *    8% metric/health — monitoring
 *    5% legacy shorten — POST /
 *    3% API v1        — POST /api/v1/shorten
 *    2% stats         — GET /api/v1/stats
 *    2% auth          — login/register/refresh
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders, buildApiHeaders, buildFormHeaders, randomAlias, isOk, isRedirect } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  scenarios: {
    redirects: {
      executor: 'ramping-vus',
      exec: 'redirectTraffic',
      stages: [
        { duration: '5m', target: 40 },
        { duration: '20m', target: 80 },
        { duration: '10m', target: 60 },
        { duration: '15m', target: 100 },
        { duration: '10m', target: 40 },
      ],
    },
    monitoring: {
      executor: 'constant-vus',
      exec: 'monitoringTraffic',
      vus: 8,
      duration: '60m',
    },
    legacy_shorten: {
      executor: 'constant-vus',
      exec: 'legacyShortenTraffic',
      vus: 5,
      duration: '60m',
    },
    api: {
      executor: 'constant-vus',
      exec: 'apiTraffic',
      vus: 3,
      duration: '60m',
    },
    stats: {
      executor: 'constant-vus',
      exec: 'statsTraffic',
      vus: 2,
      duration: '60m',
    },
    auth: {
      executor: 'constant-vus',
      exec: 'authTraffic',
      vus: 2,
      duration: '60m',
    },
  },
  thresholds: THRESHOLDS.load,
};

export function setup() {
  const testUrls = createTestUrls();
  return { testUrls };
}

// --- 80% Redirects ---
export function redirectTraffic(data) {
  const aliases = allAliases(data.testUrls);
  if (aliases.length === 0) {
    sleep(1);
    return;
  }

  const alias = randomItem(aliases);
  const res = http.get(`${BASE_URL}/${encodeURIComponent(alias)}`, {
    headers: buildHeaders(),
    redirects: 0,
  });
  check(res, {
    'redirect: status 301/302': (r) => isRedirect(r),
  });

  sleep(randomIntBetween(1, 3));
}

// --- 8% Monitoring (metric + health) ---
export function monitoringTraffic() {
  const roll = Math.random();

  if (roll < 0.6) {
    // GET /metric
    const res = http.get(`${BASE_URL}/metric`, {
      headers: { 'Accept': 'application/json' },
    });
    check(res, {
      'metric: status 200': (r) => r.status === 200,
    });
  } else {
    // GET /health
    const res = http.get(`${BASE_URL}/health`);
    check(res, {
      'health: status 200': (r) => r.status === 200,
    });
  }

  sleep(randomIntBetween(2, 5));
}

// --- 5% Legacy Shorten ---
export function legacyShortenTraffic() {
  const roll = Math.random();

  if (roll < 0.75) {
    // POST / — legacy v1 shorten
    const alias = randomAlias('k6mx');
    const res = http.post(
      `${BASE_URL}/`,
      `url=https://httpstat.us/200&alias=${alias}`,
      { headers: buildFormHeaders() },
    );
    check(res, {
      'legacy shorten: status 200': (r) => r.status === 200,
    });
  } else {
    // POST /emoji
    const res = http.post(
      `${BASE_URL}/emoji`,
      'url=https://httpstat.us/200',
      { headers: buildFormHeaders() },
    );
    check(res, {
      'emoji shorten: status 200': (r) => r.status === 200,
    });
  }

  sleep(randomIntBetween(2, 5));
}

// --- 3% API v1 ---
export function apiTraffic() {
  const alias = randomAlias('k6v2');
  const res = http.post(
    `${BASE_URL}/api/v1/shorten`,
    JSON.stringify({ url: 'https://httpstat.us/200', alias: alias }),
    { headers: buildApiHeaders() },
  );
  check(res, {
    'api shorten: status 201': (r) => r.status === 201,
  });

  sleep(randomIntBetween(3, 8));
}

// --- 2% Stats ---
export function statsTraffic(data) {
  const aliases = allAliases(data.testUrls);
  if (aliases.length === 0) {
    sleep(1);
    return;
  }

  const alias = randomItem(aliases);
  const roll = Math.random();

  if (roll < 0.7) {
    // GET /api/v1/stats
    const res = http.get(
      `${BASE_URL}/api/v1/stats?short_code=${encodeURIComponent(alias)}&scope=anon`,
      { headers: buildApiHeaders() },
    );
    check(res, {
      'stats: status 200': (r) => r.status === 200,
    });
  } else {
    // GET /api/v1/export
    const res = http.get(
      `${BASE_URL}/api/v1/export?short_code=${encodeURIComponent(alias)}&format=json&scope=anon`,
      { headers: buildApiHeaders() },
    );
    check(res, {
      'export: status 200': (r) => r.status === 200,
    });
  }

  sleep(randomIntBetween(5, 15));
}

// --- 2% Auth ---
export function authTraffic() {
  const roll = Math.random();

  if (roll < 0.35) {
    // POST /auth/login — fake credentials, expect 401
    const res = http.post(
      `${BASE_URL}/auth/login`,
      JSON.stringify({
        email: `k6user${randomIntBetween(1, 10000)}@test.invalid`,
        password: 'FakePassword123!',
      }),
      { headers: buildApiHeaders() },
    );
    check(res, {
      'auth login: status 401': (r) => r.status === 401,
    });
  } else if (roll < 0.55) {
    // POST /auth/register — unique email
    const uniqueEmail = `k6reg+${Date.now()}${randomIntBetween(1000, 9999)}@test.invalid`;
    const res = http.post(
      `${BASE_URL}/auth/register`,
      JSON.stringify({
        email: uniqueEmail,
        password: 'K6TestPass123!',
        name: 'K6 Mixed Test',
      }),
      { headers: buildApiHeaders() },
    );
    check(res, {
      'auth register: status 201 or 409': (r) => r.status === 201 || r.status === 409,
    });
  } else if (roll < 0.70) {
    // GET /auth/me — no auth, expect 401
    const res = http.get(`${BASE_URL}/auth/me`, {
      headers: buildApiHeaders(),
    });
    check(res, {
      'auth me: status 401': (r) => r.status === 401,
    });
  } else if (roll < 0.85) {
    // POST /auth/request-password-reset — timing-safe 200
    const res = http.post(
      `${BASE_URL}/auth/request-password-reset`,
      JSON.stringify({ email: 'nobody@k6test.invalid' }),
      { headers: buildApiHeaders() },
    );
    check(res, {
      'auth reset: status 200': (r) => r.status === 200,
    });
  } else {
    // POST /auth/logout
    const res = http.post(
      `${BASE_URL}/auth/logout`,
      null,
      { headers: buildApiHeaders() },
    );
    check(res, {
      'auth logout: status 200': (r) => r.status === 200,
    });
  }

  sleep(randomIntBetween(5, 15));
}
