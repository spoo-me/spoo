import { describe, expect, it } from "vitest";

import canonical from "../../../app/data/preview_bots.json";
import edgeCopy from "../contract/preview_bots.json";
import { wantsPreview } from "../src/bots";

function req(
  ua: string,
  opts: { method?: string; category?: string } = {},
): Request {
  const r = new Request("https://spoo.me/abc1234", {
    method: opts.method ?? "GET",
    headers: ua ? { "user-agent": ua } : {},
  });
  if (opts.category) {
    Object.defineProperty(r, "cf", {
      value: { verifiedBotCategory: opts.category },
    });
  }
  return r;
}

const PREVIEW_UAS = [
  "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
  "facebookexternalhit/1.1 Facebot Twitterbot/1.0", // iMessage spoof
  "WhatsApp/2.23.20.0 A", // also Signal + Primal
  "TelegramBot (like TwitterBot)",
  "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
  "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
  "Twitterbot/1.0",
  "LinkedInBot/1.0 (compatible; Mozilla/5.0)",
  "Mozilla/5.0 (Windows NT 6.1; WOW64) SkypeUriPreview Preview/0.5",
  "Bluesky Cardyb/1.1",
  "Synapse (bot; +https://github.com/matrix-org/synapse)",
];

const NON_PREVIEW_UAS = [
  "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
  "Mozilla/5.0 (compatible; bingbot/2.0)",
  "Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
  "Mozilla/5.0 (compatible; ClaudeBot/1.0)",
  "curl/8.4.0",
  "python-requests/2.31",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
  // in-app browsers are humans — the trap that burned Dub's generic list
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5) AppleWebKit/605.1.15 Line/13.5.0",
  "Mozilla/5.0 (iPhone) Pinterest for iOS/12.1",
  "",
];

describe("wantsPreview", () => {
  for (const ua of PREVIEW_UAS) {
    it(`preview: ${ua.slice(0, 40)}`, () => {
      expect(wantsPreview(req(ua))).toBe(true);
    });
  }

  for (const ua of NON_PREVIEW_UAS) {
    it(`redirect: ${ua.slice(0, 40) || "(empty UA)"}`, () => {
      expect(wantsPreview(req(ua))).toBe(false);
    });
  }

  it("HEAD is preview even with a browser UA", () => {
    expect(wantsPreview(req("Mozilla/5.0 Chrome/126.0", { method: "HEAD" }))).toBe(
      true,
    );
  });

  it("verifiedBotCategory in the recon set wins over a browser UA", () => {
    // cf_categories is empty until B1 recon fills it; simulate a filled set
    // by asserting the mechanism only when configured.
    const configured = (edgeCopy.cf_categories ?? []).length > 0;
    const category = configured ? edgeCopy.cf_categories[0] : "Page Preview";
    const result = wantsPreview(
      req("Mozilla/5.0 Chrome/126.0", { category }),
    );
    expect(result).toBe(configured);
  });
});

describe("preview_bots.json copies", () => {
  it("edge copy matches the canonical app/data/preview_bots.json", () => {
    expect(edgeCopy).toEqual(canonical);
  });
});
