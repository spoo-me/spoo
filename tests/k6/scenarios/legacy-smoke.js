/**
 * Legacy Endpoints Smoke Test
 *
 * Verifies legacy v1 endpoints respond correctly.
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

export function setup() {
  // Create a v1 URL to use for the preview page test
  const alias = randomAlias('k6lgcy');
  const res = http.post(
    `${BASE_URL}/`,
    `url=https://httpstat.us/200&alias=${alias}`,
    { headers: buildFormHeaders() },
  );

  const created = check(res, {
    'setup: legacy URL created': (r) => r.status === 200,
  });

  return { alias: created ? alias : null };
}

export default function (data) {
  const endpoint = randomIntBetween(1, 5);

  switch (endpoint) {
    case 1: {
      // POST / — legacy v1 shorten
      const alias = randomAlias('k6lg');
      const res = http.post(
        `${BASE_URL}/`,
        `url=https://httpstat.us/200&alias=${alias}`,
        { headers: buildFormHeaders() },
      );
      check(res, {
        'POST / legacy shorten: status 200': (r) => r.status === 200,
        'POST / legacy shorten: is JSON': (r) =>
          r.headers['Content-Type'] && r.headers['Content-Type'].includes('application/json'),
      });
      break;
    }

    case 2: {
      // POST /emoji — emoji shorten
      const res = http.post(
        `${BASE_URL}/emoji`,
        'url=https://httpstat.us/200',
        { headers: buildFormHeaders() },
      );
      check(res, {
        'POST /emoji: status 200': (r) => r.status === 200,
        'POST /emoji: is JSON': (r) =>
          r.headers['Content-Type'] && r.headers['Content-Type'].includes('application/json'),
      });
      break;
    }

    case 3: {
      // GET /metric — global metrics
      const res = http.get(`${BASE_URL}/metric`, {
        headers: { 'Accept': 'application/json' },
      });
      check(res, {
        'GET /metric: status 200': (r) => r.status === 200,
        'GET /metric: is JSON': (r) =>
          r.headers['Content-Type'] && r.headers['Content-Type'].includes('application/json'),
      });
      break;
    }

    case 4: {
      // GET /stats — stats form page
      const res = http.get(`${BASE_URL}/stats`);
      check(res, {
        'GET /stats: status 200': (r) => r.status === 200,
        'GET /stats: is HTML': (r) =>
          r.headers['Content-Type'] && r.headers['Content-Type'].includes('text/html'),
      });
      break;
    }

    case 5: {
      // GET /{code}+ — preview page
      if (data.alias) {
        const res = http.get(`${BASE_URL}/${data.alias}+`, {
          redirects: 0,
        });
        check(res, {
          'GET /{code}+ preview: status 200': (r) => r.status === 200,
          'GET /{code}+ preview: is HTML': (r) =>
            r.headers['Content-Type'] && r.headers['Content-Type'].includes('text/html'),
        });
      }
      break;
    }
  }

  sleep(randomIntBetween(1, 3));
}
