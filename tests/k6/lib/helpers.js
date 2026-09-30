import { randomItem, randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { userAgents, botUserAgents, ipLocations, referrers } from './data.js';

// Generate a random fake IP
export function generateFakeIp() {
  const octet = () => randomIntBetween(1, 255);
  return `${octet()}.${octet()}.${octet()}.${octet()}`;
}

// Build realistic request headers
// Options: { includeReferer: true/false (default 30% chance), bot: true/false (default false), useGeoIp: true/false (default true) }
export function buildHeaders(opts = {}) {
  const bot = opts.bot || false;
  const useGeoIp = opts.useGeoIp !== false; // default true
  const refererChance = opts.refererChance || 0.3;

  const ua = bot ? randomItem(botUserAgents) : randomItem(userAgents);
  const location = useGeoIp ? randomItem(ipLocations) : null;

  const headers = {
    'User-Agent': ua,
    'X-Forwarded-For': location ? location.ip : generateFakeIp(),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
  };

  if (Math.random() < refererChance) {
    headers['Referer'] = randomItem(referrers);
  }

  return headers;
}

// Build JSON API headers (for POST /api/v1/shorten etc.)
export function buildApiHeaders(token = null) {
  const headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

// Build form headers (for legacy POST / etc.)
export function buildFormHeaders() {
  return {
    'Content-Type': 'application/x-www-form-urlencoded',
    'Accept': 'application/json',
  };
}

// Standard check: response is a redirect (301 or 302)
export function isRedirect(response) {
  return response.status === 301 || response.status === 302;
}

// Standard check: response is a success (2xx)
export function isOk(response) {
  return response.status >= 200 && response.status < 300;
}

// Generate a random alias for URL creation
export function randomAlias(prefix = 'k6') {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let result = `${prefix}-`;
  for (let i = 0; i < 8; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}
