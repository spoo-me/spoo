import http from 'k6/http';
import { check } from 'k6';
import { BASE_URL } from './lib/config.js';

// Creates test URLs for the test run. Call from scenario's setup() function.
// Returns an object with aliases that can be used in the default function.
export function createTestUrls() {
  const testUrls = {
    v2: [],      // v2 aliases (7-char)
    v1: [],      // v1 aliases (6-char)
    emoji: [],   // emoji aliases
    passwordProtected: null,  // alias with password
    maxClicks: null,          // alias with max_clicks
  };

  // Create 5 v2 URLs via API
  for (let i = 0; i < 5; i++) {
    const alias = `k6v2${String(i).padStart(4, '0')}${Date.now().toString(36).slice(-3)}`;
    const res = http.post(`${BASE_URL}/api/v1/shorten`, JSON.stringify({
      url: 'https://httpstat.us/200',
      alias: alias,
    }), { headers: { 'Content-Type': 'application/json' } });

    if (check(res, { 'v2 URL created': (r) => r.status === 201 })) {
      testUrls.v2.push(res.json().alias);
    }
  }

  // Create 3 v1 URLs via legacy endpoint
  for (let i = 0; i < 3; i++) {
    const alias = `k6v1${String(i).padStart(2, '0')}${Date.now().toString(36).slice(-3)}`;
    const res = http.post(`${BASE_URL}/`, `url=https://httpstat.us/200&alias=${alias}`, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json' },
    });

    if (check(res, { 'v1 URL created': (r) => r.status === 200 })) {
      testUrls.v1.push(alias);
    }
  }

  // Create 2 legacy emoji URLs (exercises the tightened legacy path)
  for (let i = 0; i < 2; i++) {
    const res = http.post(`${BASE_URL}/emoji`, `url=https://httpstat.us/200`, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json' },
    });

    if (check(res, { 'emoji URL created': (r) => r.status === 200 })) {
      const data = res.json();
      testUrls.emoji.push(data.emoji_alias || data.short_url.split('/').pop());
    }
  }

  // Create 2 v2 emoji URLs (auto-generated via alias_type)
  for (let i = 0; i < 2; i++) {
    const res = http.post(`${BASE_URL}/api/v1/shorten`, JSON.stringify({
      url: 'https://httpstat.us/200',
      alias_type: 'emoji',
    }), { headers: { 'Content-Type': 'application/json' } });

    if (check(res, { 'v2 emoji URL created': (r) => r.status === 201 })) {
      testUrls.emoji.push(res.json().alias);
    }
  }

  // Create a password-protected v2 URL
  const pwAlias = `k6pw${Date.now().toString(36).slice(-5)}`;
  const pwRes = http.post(`${BASE_URL}/api/v1/shorten`, JSON.stringify({
    url: 'https://httpstat.us/200',
    alias: pwAlias,
    password: 'TestPass123!',
  }), { headers: { 'Content-Type': 'application/json' } });

  if (check(pwRes, { 'password URL created': (r) => r.status === 201 })) {
    testUrls.passwordProtected = { alias: pwAlias, password: 'TestPass123!' };
  }

  // Create a max-clicks URL (high limit so it doesn't expire during test)
  const mcAlias = `k6mc${Date.now().toString(36).slice(-5)}`;
  const mcRes = http.post(`${BASE_URL}/api/v1/shorten`, JSON.stringify({
    url: 'https://httpstat.us/200',
    alias: mcAlias,
    max_clicks: 999999,
  }), { headers: { 'Content-Type': 'application/json' } });

  if (check(mcRes, { 'max-clicks URL created': (r) => r.status === 201 })) {
    testUrls.maxClicks = mcAlias;
  }

  console.log(`Setup complete: ${testUrls.v2.length} v2, ${testUrls.v1.length} v1, ${testUrls.emoji.length} emoji URLs created`);
  return testUrls;
}

// Convenience: get all redirect-testable aliases as a flat array
export function allAliases(testUrls) {
  const aliases = [...testUrls.v2, ...testUrls.v1];
  if (testUrls.maxClicks) aliases.push(testUrls.maxClicks);
  // emoji aliases are URL-encoded, include them too
  aliases.push(...testUrls.emoji);
  return aliases;
}
