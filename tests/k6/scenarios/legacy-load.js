/**
 * Legacy Endpoints Load Test
 *
 * Load tests legacy endpoints with weighted distribution.
 * 100 VUs, 5 minutes (30s ramp-up, 4m hold, 30s ramp-down).
 *
 * Distribution:
 *   50% GET /metric (cached, fast)
 *   30% GET /{code} redirect
 *   15% POST / (legacy shorten)
 *    5% POST /emoji
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders, buildApiHeaders, buildFormHeaders, randomAlias, isOk, isRedirect } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  stages: [
    { duration: '1m',  target: 100 },
    { duration: '10m', target: 100 },
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

  if (roll < 0.50) {
    // 50% — GET /metric
    const res = http.get(`${BASE_URL}/metric`, {
      headers: { 'Accept': 'application/json' },
    });
    check(res, {
      'metric: status 200': (r) => r.status === 200,
    });
  } else if (roll < 0.80) {
    // 30% — GET /{code} redirect
    if (aliases.length > 0) {
      const alias = randomItem(aliases);
      const res = http.get(`${BASE_URL}/${encodeURIComponent(alias)}`, {
        headers: buildHeaders(),
        redirects: 0,
      });
      check(res, {
        'redirect: status 301 or 302': (r) => isRedirect(r),
      });
    }
  } else if (roll < 0.95) {
    // 15% — POST / (legacy shorten)
    const alias = randomAlias('k6ll');
    const res = http.post(
      `${BASE_URL}/`,
      `url=https://httpstat.us/200&alias=${alias}`,
      { headers: buildFormHeaders() },
    );
    check(res, {
      'legacy shorten: status 200': (r) => r.status === 200,
    });
  } else {
    // 5% — POST /emoji
    const res = http.post(
      `${BASE_URL}/emoji`,
      'url=https://httpstat.us/200',
      { headers: buildFormHeaders() },
    );
    check(res, {
      'emoji shorten: status 200': (r) => r.status === 200,
    });
  }

  sleep(randomIntBetween(1, 2));
}
