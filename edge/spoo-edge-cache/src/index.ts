/**
 * spoo-edge-cache — pure-reader edge cache for hot short URLs.
 *
 * The Worker makes NO decisions: origin promotes eligible hot URLs into
 * KV (app/services/edge_cache/) and this Worker serves
 * whatever it finds there. Miss, excluded path, unknown entry type, or
 * any internal error → passthrough to origin, which is exactly today's
 * request path. Entries self-expire via KV TTL (invalidation v1).
 *
 * Contract (key format + entry JSON) is pinned by ../contract/ and
 * tested from both sides — change only in lockstep.
 */

import { wantsPreview } from "./bots";

interface EdgeCacheEntry {
  type: string;
  url?: string;
  status?: number;
  /** geo_redirect only: ISO alpha-2 country code → destination override. */
  rules?: Record<string, string>;
  og_html?: string;
}

/** Path prefixes that are never short codes — skip KV entirely. */
const EXCLUDED_PREFIXES = [
  "/api/",
  "/dashboard/",
  "/auth/",
  "/oauth/",
  "/static/",
  "/stats/",
  "/relay/",
  "/onboarding/",
];

/**
 * Single-segment page paths that are never short codes — the reserved
 * alias list (app/shared/reserved_aliases.py) guarantees no code can take
 * these names, so skipping KV is free. Exact matches only: "aboutus"
 * is a legal alias and keeps its lookup. Drift from the Python list is
 * benign — a missing entry costs one wasted KV read, nothing more.
 */
const EXCLUDED_EXACT = new Set([
  "about",
  "api",
  "apps",
  "auth",
  "billing",
  "blog",
  "brand",
  "callback",
  "changelog",
  "contact",
  "dashboard",
  "discord",
  "docs",
  "domains",
  "emoji",
  "error",
  "export",
  "favicon",
  "features",
  "forgot-password",
  "geo",
  "github",
  "health",
  "help",
  "home",
  "humans",
  "icon",
  "icons-3d",
  "images",
  "keys",
  "legal",
  "links",
  "login",
  "logout",
  "metric",
  "oauth",
  "onboarding",
  "pricing",
  "privacy",
  "privacy-policy",
  "profile",
  "profile-pictures",
  "public",
  "register",
  "relay",
  "report",
  "reset",
  "result",
  "robots",
  "security",
  "settings",
  "signin",
  "signup",
  "sitemap",
  "statistics",
  "stats",
  "static",
  "terms",
  "terms-of-service",
  "testimonials",
  "tos",
  "twitter",
  "verify",
]);

/**
 * KV key for this request, or null when the request can never be a
 * cached short-code redirect (wrong method, excluded path, password
 * attempt, nested path, file-ish path).
 */
export function lookupKey(request: Request): string | null {
  if (request.method !== "GET" && request.method !== "HEAD") return null;

  const url = new URL(request.url);
  // Password attempts must always reach origin for verification.
  if (url.searchParams.has("password")) return null;
  // ?bot=1 is the meta-tags debug view — origin renders it with the JS
  // auto-redirect disabled so developers can inspect the crawler page.
  if (url.searchParams.has("bot")) return null;

  const path = url.pathname;
  if (path === "/" || path.includes(".")) return null;
  if (EXCLUDED_PREFIXES.some((prefix) => path.startsWith(prefix))) return null;

  const code = path.slice(1);
  if (code.length === 0 || code.includes("/")) return null;
  // /{code}+ is preview intent — + is never a legal alias character.
  if (code.endsWith("+") || code.endsWith("%2B")) return null;
  if (EXCLUDED_EXACT.has(code)) return null;

  // Promotion writes keys with the canonical host: lowercase, no www.
  const host = url.hostname.toLowerCase().replace(/^www\./, "");
  // Emoji codes arrive percent-encoded; keys store raw characters.
  return `cache:${host}:${decodeURIComponent(code)}`;
}

export default {
  async fetch(request, env, ctx): Promise<Response> {
    try {
      const key = lookupKey(request);
      if (key === null) return passthrough(request, env);

      const entry = await env.EDGE_CACHE.get<EdgeCacheEntry>(key, "json");
      if (entry === null) return passthrough(request, env);

      // Custom meta-tags: preview crawlers get the prerendered OG page —
      // from og_only entries (eager write-through) and from hot redirect /
      // geo_redirect entries carrying og_html. Everyone else falls through
      // to the redirect branch (og_only for humans = passthrough, so
      // origin keeps click tracking for non-hot og-links).
      if (typeof entry.og_html === "string" && wantsPreview(request)) {
        console.log(
          JSON.stringify({ event: "edge_og_hit", key, colo: request.cf?.colo }),
        );
        return new Response(entry.og_html, {
          status: 200,
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "X-Spoo-Edge": "hit",
          },
        });
      }

      if (typeof entry.url !== "string") {
        // og_only for humans — origin keeps click tracking.
        return passthrough(request, env);
      }

      const isGeo = entry.type === "geo_redirect";
      if (isGeo && (typeof entry.rules !== "object" || entry.rules === null)) {
        // Schema-invalid geo entry — origin always knows how to answer.
        return passthrough(request, env);
      }
      if (!isGeo && entry.type !== "redirect") {
        // An entry type this Worker version doesn't know — passthrough
        // (forward compatibility, pinned by the malformed fixtures).
        return passthrough(request, env);
      }

      // Same CF geodata origin reads from CF-IPCountry, so an edge-served
      // geo decision can never disagree with an origin-served one.
      const country = isGeo
        ? (request.cf?.country ?? "").toString().toUpperCase()
        : null;
      const override =
        isGeo && country !== null ? entry.rules?.[country] : undefined;
      const location = typeof override === "string" ? override : entry.url;

      console.log(
        JSON.stringify({
          event: "edge_hit",
          key,
          colo: request.cf?.colo,
          // Recon for meta-tags preview serving: learn the real runtime
          // verifiedBotCategory strings on this zone before enforcing them
          // (docs say "Page Preview"; one report says "Preview"; Slackbot
          // is categorized "Webhooks"). Field is absent from workers-types.
          botCategory: (request.cf as { verifiedBotCategory?: string } | undefined)
            ?.verifiedBotCategory,
          ua: request.headers.get("user-agent") ?? "",
          ...(isGeo && { geo: true, country, matched: override !== undefined }),
        }),
      );
      const headers: Record<string, string> = {
        Location: location,
        "X-Robots-Tag": "noindex, nofollow, noarchive",
        "X-Spoo-Edge": "hit",
      };
      // Response varies per visitor country — nothing may cache it.
      if (isGeo) headers["Cache-Control"] = "no-store";
      return new Response(null, {
        status: entry.status === 301 ? 301 : 302,
        headers,
      });
    } catch (err) {
      // Fail-open: worst case is exactly today's request path. Explicit
      // catch (not passThroughOnException) so the error is visible.
      console.error(
        JSON.stringify({ event: "edge_cache_error", error: String(err) }),
      );
      return passthrough(request, env);
    }
  },
} satisfies ExportedHandler<Env>;

/**
 * Continue to origin. ORIGIN_OVERRIDE exists only in local dev
 * (.dev.vars) so `wrangler dev` can target a local compose app;
 * deployed environments never define it and take the plain
 * fetch(request) path through CF's normal proxy chain.
 */
function passthrough(request: Request, env: Env): Promise<Response> {
  if (env.ORIGIN_OVERRIDE) {
    const url = new URL(request.url);
    const origin = new URL(env.ORIGIN_OVERRIDE);
    url.protocol = origin.protocol;
    url.host = origin.host;
    return fetch(new Request(url, request));
  }
  return fetch(request);
}
