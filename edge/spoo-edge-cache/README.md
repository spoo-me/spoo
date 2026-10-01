# spoo-edge-cache

Pure-reader Cloudflare Worker: serves hot short-URL redirects straight
from KV at the PoP. Origin decides *what* gets cached (promotion via the
hotness consumer — `app/services/edge_cache/`); this
Worker only reads. Miss, excluded path, unknown entry type, or any
internal error → passthrough to origin, byte-identical to having no
Worker at all. Design doc: `thoughts/cf-edge-cache-v2.md` (spoo-latest).

**This component is CF-deployment-only.** Self-hosters without
Cloudflare never deploy it and need nothing from this directory. With
their own CF zone it works as-is: deploy with your own routes, point
your origin's `EDGE_CACHE_*` env at your namespace.

## The contract

`contract/fixtures.json` pins the KV key format
(`cache:{domain}:{short_code}`, lowercase host, no `www.`) and the entry
JSON (`entry.schema.json`). Both sides test against it:

- Python: `tests/unit/services/test_edge_contract.py` (emission)
- Worker: `test/index.spec.ts` (serving)

Change key format or entry shape ONLY by updating fixtures + schema +
both suites in the same commit.

## Commands

```bash
npm install            # toolchain (contained in this directory)
npm test               # vitest in real workerd, KV simulated
npm run check          # wrangler types --check + tsc + vitest
npm run dev            # local worker on :8787 (local KV simulation)
npm run deploy:beta    # → beta.spoo.me/*
npm run deploy:production
```

## Local FULL loop — automatic promotion (zero cloud)

The real promotion action can write into wrangler dev's local KV via the
Explorer API (`EDGE_CACHE_API_BASE` override). Hot URLs then promote
automatically during local load tests — see `deploy/docker-compose.edge-dev.yml`
at the repo root for the three-step setup. Requires
`127.0.0.1 spoo.local` in `/etc/hosts` (the local system domain) so the
Worker computes the same `cache:spoo.local:{code}` keys the promotion
action writes.

## Local dev loop (manual seeding, zero cloud)

```bash
# 1. run the app locally
docker compose up -d

# 2. run the worker (the dev script passes ORIGIN_OVERRIDE=http://localhost:8000
#    as a CLI --var; avoid .dev.vars — it changes `wrangler types` output
#    and breaks the CI types check)
npm run dev
npx wrangler kv key put --binding EDGE_CACHE --local \
  "cache:spoo.me:abc1234" \
  '{"type":"redirect","url":"https://example.com","status":302}'

curl -sI 'http://localhost:8787/abc1234' | grep -iE 'location|x-spoo-edge'
```

While `wrangler dev` runs, local resources are also inspectable over
HTTP via the Explorer API — a local mirror of the same CF REST surface
`CloudflareKVClient` uses in production:

```bash
BASE=http://localhost:8787/cdn-cgi/explorer/api
curl "$BASE/storage/kv/namespaces/edge-cache-local/keys?prefix=cache:"
curl "$BASE/storage/kv/namespaces/edge-cache-local/values/cache:localhost:abc1234"
```

## Provisioning (one-time per environment)

KV `id` is omitted in `wrangler.jsonc` env blocks: the first
`wrangler deploy --env <env>` auto-creates the namespace and writes the
id back into the file — commit that diff (lockfile semantics), then copy
the id into the origin's `EDGE_CACHE_KV_NAMESPACE_ID`. If your wrangler
version refuses to deploy without an id: `wrangler kv namespace create
EDGE_CACHE-<env>`, paste the id, same outcome.

Origin env (all three or the feature stays off):

```bash
EDGE_CACHE_CF_ACCOUNT_ID=...
EDGE_CACHE_CF_API_TOKEN=...      # Workers KV write scope only
EDGE_CACHE_KV_NAMESPACE_ID=...   # from wrangler.jsonc after first deploy
```

## Operations

- Logs: `wrangler tail --env production` (structured JSON; sampled at
  5% in production via `observability.head_sampling_rate`)
- Manual promote/demote (emergency + abuse takedown):
  `wrangler kv key delete --env production --binding EDGE_CACHE "cache:spoo.me:<code>"`
- Rollback: `wrangler rollback` (previous version) or `wrangler delete`
  (removes worker + routes; zone reverts to no-Worker behavior)
