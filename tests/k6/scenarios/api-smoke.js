/**
 * API v1 Smoke Test
 *
 * Exercises all API v1 endpoints with minimal load to verify they respond correctly.
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
  // Create a test URL to use for stats/export queries
  const alias = randomAlias('k6smoke');
  const res = http.post(
    `${BASE_URL}/api/v1/shorten`,
    JSON.stringify({ url: 'https://httpstat.us/200', alias: alias }),
    { headers: buildApiHeaders() },
  );

  const created = check(res, {
    'setup: URL created': (r) => r.status === 201,
  });

  return { alias: created ? alias : null };
}

export default function (data) {
  const endpoint = randomIntBetween(1, 5);

  switch (endpoint) {
    case 1: {
      // POST /api/v1/shorten
      const alias = randomAlias('k6api');
      const res = http.post(
        `${BASE_URL}/api/v1/shorten`,
        JSON.stringify({ url: 'https://httpstat.us/200', alias: alias }),
        { headers: buildApiHeaders() },
      );
      check(res, {
        'POST /api/v1/shorten status 201': (r) => r.status === 201,
        'POST /api/v1/shorten has alias': (r) => r.json().alias !== undefined,
      });
      break;
    }

    case 2: {
      // GET /api/v1/stats
      if (data.alias) {
        const res = http.get(
          `${BASE_URL}/api/v1/stats?short_code=${data.alias}&scope=anon`,
          { headers: buildApiHeaders() },
        );
        check(res, {
          'GET /api/v1/stats status 200': (r) => r.status === 200,
          'GET /api/v1/stats is JSON': (r) => r.headers['Content-Type'] && r.headers['Content-Type'].includes('application/json'),
        });
      }
      break;
    }

    case 3: {
      // GET /api/v1/export
      if (data.alias) {
        const res = http.get(
          `${BASE_URL}/api/v1/export?short_code=${data.alias}&format=json&scope=anon`,
          { headers: buildApiHeaders() },
        );
        check(res, {
          'GET /api/v1/export status 200': (r) => r.status === 200,
        });
      }
      break;
    }

    case 4: {
      // GET /health
      const res = http.get(`${BASE_URL}/health`);
      check(res, {
        'GET /health status 200': (r) => r.status === 200,
        'GET /health body ok': (r) => {
          const body = r.json();
          return body.status === 'healthy' || body.status === 'ok';
        },
      });
      break;
    }

    case 5: {
      // GET /metric
      const res = http.get(`${BASE_URL}/metric`, {
        headers: { 'Accept': 'application/json' },
      });
      check(res, {
        'GET /metric status 200': (r) => r.status === 200,
        'GET /metric is JSON': (r) => r.headers['Content-Type'] && r.headers['Content-Type'].includes('application/json'),
      });
      break;
    }
  }

  sleep(randomIntBetween(1, 3));
}
