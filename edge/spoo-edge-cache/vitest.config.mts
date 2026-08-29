import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

// Tests run inside actual workerd with the KV binding simulated from
// wrangler.jsonc — the closest thing to production short of a PoP.
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      // A developer's .dev.vars (ORIGIN_OVERRIDE for `wrangler dev`)
      // must not leak into tests — passthrough behavior is under test
      // and expects the plain fetch(request) path.
      miniflare: { bindings: { ORIGIN_OVERRIDE: "" } },
    }),
  ],
});
