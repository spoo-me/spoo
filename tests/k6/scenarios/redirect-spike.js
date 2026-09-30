import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  stages: [
    { duration: '5m',  target: 50 },   // warm up
    { duration: '1m',  target: 500 },   // spike!
    { duration: '5m',  target: 500 },   // hold spike
    { duration: '1m',  target: 50 },    // recover
    { duration: '5m',  target: 50 },    // verify recovery
    { duration: '1m',  target: 0 },     // ramp down
  ],
  thresholds: THRESHOLDS.spike,
};

export function setup() {
  return createTestUrls();
}

export default function (data) {
  const aliases = allAliases(data);
  const alias = randomItem(aliases);

  const res = http.get(`${BASE_URL}/${alias}`, {
    headers: buildHeaders({ referrerChance: 0.3 }),
    redirects: 0,
  });

  check(res, {
    'is redirect (301/302)': (r) => r.status === 301 || r.status === 302,
    'has Location header': (r) => !!r.headers['Location'],
    'not server error': (r) => r.status < 500,
    'response time < 3s': (r) => r.timings.duration < 3000,
  });

  sleep(randomIntBetween(1, 2));
}
