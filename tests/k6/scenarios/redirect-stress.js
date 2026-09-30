import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  stages: [
    { duration: '2m',  target: 100 },
    { duration: '3m',  target: 300 },
    { duration: '3m',  target: 500 },
    { duration: '3m',  target: 750 },
    { duration: '3m',  target: 1000 },
    { duration: '3m',  target: 1500 },
    { duration: '5m',  target: 1500 },  // hold at peak
    { duration: '3m',  target: 0 },     // ramp down
  ],
  thresholds: THRESHOLDS.stress,
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
    'response time < 5s': (r) => r.timings.duration < 5000,
  });

  sleep(randomIntBetween(500, 1000) / 1000);
}
