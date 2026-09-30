# k6 Performance Tests

Performance test suite for spoo.me URL shortener using [k6](https://k6.io/).

## Prerequisites

- [k6](https://k6.io/docs/get-started/installation/) installed
- Application running locally (`uvicorn` or `docker compose up`)

## Test Types

| Type | Purpose | VUs | Duration |
|------|---------|-----|----------|
| **Smoke** | Sanity check — app works under minimal load | 1-5 | 30s |
| **Load** | Sustained traffic — normal production patterns | 100-500 | 10m |
| **Stress** | Push to breaking point — find capacity limits | Ramp to 1000+ | 15m |
| **Spike** | Sudden traffic burst — test recovery | Normal → 5x → Normal | 10m |
| **Soak** | Long-running — detect memory leaks, degradation | 50 | 30m |

## Quick Start

```bash
# Smoke test (quick sanity check)
k6 run tests/k6/scenarios/redirect-smoke.js

# Load test
k6 run tests/k6/scenarios/redirect-load.js

# Against a remote server
K6_BASE_URL=https://staging.spoo.me k6 run tests/k6/scenarios/redirect-smoke.js
```

## Scenarios

### Redirect Tests
The hot path — `GET /{short_code}` at ~400k req/day in production.

- `redirect-smoke.js` — 5 VUs, 30s
- `redirect-load.js` — 200 VUs, 10m sustained
- `redirect-stress.js` — Ramp to 1000 VUs, find breaking point
- `redirect-spike.js` — 50 → 500 → 50 VUs, test recovery
- `redirect-soak.js` — 50 VUs, 30m, detect degradation

### API v1 Tests
JSON API endpoints (`/api/v1/*`).

- `api-smoke.js` — All API endpoints, 2 VUs, 30s
- `api-load.js` — Shorten + stats + management, 50 VUs, 5m

### Auth Tests
Authentication endpoints (`/auth/*`).

- `auth-smoke.js` — Login, register, refresh, 2 VUs, 30s

### Legacy Tests
Legacy v1 endpoints (`POST /`, `/emoji`, `/metric`).

- `legacy-smoke.js` — All legacy endpoints, 2 VUs, 30s
- `legacy-load.js` — Metric + redirect mix, 100 VUs, 5m

### Mixed Realistic
Weighted traffic simulation matching production patterns.

- `mixed-realistic.js` — 60min with realistic traffic distribution

## Test Data

Tests auto-create URLs in k6's `setup()` function — no manual setup needed. URLs are created via the API using `https://httpstat.us/200` as the target (lightweight mock).

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `K6_BASE_URL` | `http://localhost:8000` | Base URL of the app |

## Thresholds

All tests include pass/fail thresholds:
- **Smoke**: p95 < 500ms, error rate < 1%
- **Load**: p95 < 2s, error rate < 1%
- **Stress**: p95 < 5s, error rate < 5%
- **Spike**: p95 < 3s, error rate < 5%
- **Soak**: p95 < 2s, error rate < 1%

## Architecture

```
tests/k6/
├── lib/
│   ├── config.js    # BASE_URL, thresholds
│   ├── data.js      # User agents, IPs, referrers (SharedArray)
│   └── helpers.js   # Header builders, check helpers
├── setup.js         # Auto URL creation for tests
├── scenarios/       # All test scripts
└── README.md
```
