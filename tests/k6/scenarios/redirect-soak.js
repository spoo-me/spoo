import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  vus: 50,
  duration: '1h',
  thresholds: Object.assign({}, THRESHOLDS.soak, {
    'http_req_duration{expected_response:true}': ['p(50)<500', 'p(90)<1500', 'p(95)<2000', 'p(99)<5000'],
  }),
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
    'response time < 2s': (r) => r.timings.duration < 2000,
    'no memory-related errors': (r) => r.status !== 503,
  });

  sleep(randomIntBetween(2, 5));
}
