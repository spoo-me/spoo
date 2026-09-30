import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomItem } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { BASE_URL, THRESHOLDS } from '../lib/config.js';
import { buildHeaders } from '../lib/helpers.js';
import { createTestUrls, allAliases } from '../setup.js';

export const options = {
  vus: 5,
  duration: '2m',
  thresholds: THRESHOLDS.smoke,
};

export function setup() {
  return createTestUrls();
}

export default function (data) {
  const aliases = allAliases(data);
  const alias = aliases[Math.floor(Math.random() * aliases.length)];
  const res = http.get(`${BASE_URL}/${alias}`, {
    headers: buildHeaders(),
    redirects: 0,
  });
  check(res, {
    'is redirect (301/302)': (r) => r.status === 301 || r.status === 302,
    'has Location header': (r) => !!r.headers['Location'],
    'has X-Robots-Tag': (r) => !!r.headers['X-Robots-Tag'],
  });
  sleep(1);
}
