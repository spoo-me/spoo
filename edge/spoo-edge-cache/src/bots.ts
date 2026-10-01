/**
 * Preview-crawler classification for custom meta-tags serving.
 *
 * Mirror of wants_preview() in app/services/click/bot_detection.py — both
 * runtimes consume the same JSON (../contract/preview_bots.json, byte-
 * identical to app/data/preview_bots.json; pinned by tests on both sides).
 *
 * Positive allowlist only — NOT generic bot detection. A missed preview
 * bot follows the redirect and shows the destination's own tags (today's
 * behavior); a false positive is prevented by never matching in-app
 * browser tokens (see the JSON's comment).
 */

import previewBots from "../contract/preview_bots.json";

const esc = (t: string) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const PREVIEW_RE = new RegExp(previewBots.tokens.map(esc).join("|"), "i");
const PREVIEW_CS_RE = new RegExp(previewBots.tokens_cs.map(esc).join("|"));

// Cloudflare's verified-bot signal (available on all plans, absent from
// workers-types). Enforced values come from log-only recon on the real
// zone (see cf_categories in the JSON) — empty until recon fills it, at
// which point verified bots with browser-like UAs are caught too.
const PREVIEW_CATEGORIES = new Set<string>(previewBots.cf_categories ?? []);

/** Should this request get the prerendered OG page instead of the redirect? */
export function wantsPreview(request: Request): boolean {
  // Real users always GET; link expanders and email scanners HEAD.
  if (request.method === "HEAD") return true;
  const category = (
    request.cf as { verifiedBotCategory?: string } | undefined
  )?.verifiedBotCategory;
  if (category && PREVIEW_CATEGORIES.has(category)) return true;
  const ua = request.headers.get("user-agent") ?? "";
  if (ua.length === 0) return false;
  return PREVIEW_RE.test(ua) || PREVIEW_CS_RE.test(ua);
}
