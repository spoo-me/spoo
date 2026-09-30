/**
 * API v1 Load Test
 *
 * Load tests API endpoints with weighted distribution.
 * 50 VUs, 5 minutes (30s ramp-up, 4m hold, 30s ramp-down).
 *
 * Distribution:
 *   40% POST /api/v1/shorten
 *   30% GET /api/v1/stats
 *   20% GET /health
 *   10% GET /api/v1/export
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders, buildApiHeaders, buildFormHeaders, randomAlias, isOk, isRedirect } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  stages: [
    { duration: '1m',  target: 50 },
    { duration: '10m', target: 50 },
    { duration: '1m',  target: 0 },
  ],
  thresholds: THRESHOLDS.load,
};

export function setup() {
  const testUrls = createTestUrls();
  return { testUrls };
}

export default function (data) {
  const aliases = allAliases(data.testUrls);
  const roll = Math.random();

  if (roll < 0.40) {
    // 40% — POST /api/v1/shorten
    const alias = randomAlias('k6ld');
    const res = http.post(
      `${BASE_URL}/api/v1/shorten`,
      JSON.stringify({ url: 'https://httpstat.us/200', alias: alias }),
      { headers: buildApiHeaders() },
    );
    check(res, {
      'shorten: status 201': (r) => r.status === 201,
      'shorten: has alias': (r) => {
        try { return r.json().alias !== undefined; } catch (_) { return false; }
      },
    });
  } else if (roll < 0.70) {
    // 30% — GET /api/v1/stats
    const alias = aliases.length > 0 ? randomItem(aliases) : null;
    if (alias) {
      const res = http.get(
        `${BASE_URL}/api/v1/stats?short_code=${encodeURIComponent(alias)}&scope=anon`,
        { headers: buildApiHeaders() },
      );
      check(res, {
        'stats: status 200': (r) => r.status === 200,
      });
    }
  } else if (roll < 0.90) {
    // 20% — GET /health
    const res = http.get(`${BASE_URL}/health`);
    check(res, {
      'health: status 200': (r) => r.status === 200,
    });
  } else {
    // 10% — GET /api/v1/export
    const alias = aliases.length > 0 ? randomItem(aliases) : null;
    if (alias) {
      const res = http.get(
        `${BASE_URL}/api/v1/export?short_code=${encodeURIComponent(alias)}&format=json&scope=anon`,
        { headers: buildApiHeaders() },
      );
      check(res, {
        'export: status 200': (r) => r.status === 200,
      });
    }
  }

  sleep(randomIntBetween(1, 3));
}
