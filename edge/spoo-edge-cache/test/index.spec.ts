import {
  createExecutionContext,
  env,
  waitOnExecutionContext,
} from "cloudflare:test";
import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import fixtures from "../contract/fixtures.json";
import worker, { lookupKey } from "../src/index";

// onUnhandledRequest: "error" is the disableNetConnect() of MSW — any
// outbound fetch a test didn't arm via expectOriginFetch fails loudly.
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

/** Stub the next passthrough to origin and tag it so tests can assert
 * "this request reached origin" unambiguously. */
function expectOriginFetch(
  host = "https://spoo.me",
  path: string | RegExp = /.*/,
  method = "GET",
) {
  server.use(
    http.all(
      `${host}/*`,
      ({ request }) => {
        const url = new URL(request.url);
        const pathMatches =
          typeof path === "string"
            ? url.pathname === path
            : path.test(url.pathname);
        if (request.method !== method || !pathMatches) return undefined;
        return HttpResponse.text("origin-served");
      },
      { once: true },
    ),
  );
}

// The handler is typed for edge-ingress requests (IncomingRequestCfProperties);
// the cast below is the pattern CF's own worker templates use in tests.
type IncomingRequest = Request<unknown, IncomingRequestCfProperties>;

async function dispatch(request: Request): Promise<Response> {
  const ctx = createExecutionContext();
  const response = await worker.fetch(request as IncomingRequest, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

/** Build a request carrying request.cf.country like real edge ingress.
 * country=null simulates CF not populating the field. */
function geoRequest(url: string, country: string | null): Request {
  const cf = country === null ? {} : { country };
  return new Request(url, { cf } as RequestInit);
}

describe("contract fixtures", () => {
  for (const fixture of fixtures.entries) {
    it(`serves: ${fixture.name}`, async () => {
      await env.EDGE_CACHE.put(fixture.key, JSON.stringify(fixture.value));
      const code = fixture.key.split(":").slice(2).join(":");
      const response = await dispatch(
        new Request(`https://spoo.me/${encodeURIComponent(code)}`),
      );

      expect(response.status).toBe(fixture.expect.status);
      expect(response.headers.get("Location")).toBe(fixture.expect.location);
      expect(response.headers.get("X-Spoo-Edge")).toBe("hit");
      expect(response.headers.get("X-Robots-Tag")).toContain("noindex");
    });
  }

  for (const fixture of fixtures.geo_entries) {
    describe(`serves: ${fixture.name}`, () => {
      for (const geoCase of fixture.cases) {
        const label = geoCase.country ?? "(cf.country missing)";
        it(`${label} → ${geoCase.location}`, async () => {
          await env.EDGE_CACHE.put(fixture.key, JSON.stringify(fixture.value));
          const code = fixture.key.split(":").slice(2).join(":");
          const response = await dispatch(
            geoRequest(
              `https://spoo.me/${encodeURIComponent(code)}`,
              geoCase.country,
            ),
          );

          expect(response.status).toBe(fixture.value.status);
          expect(response.headers.get("Location")).toBe(geoCase.location);
          expect(response.headers.get("X-Spoo-Edge")).toBe("hit");
          // Geo responses vary per visitor — never cacheable downstream.
          expect(response.headers.get("Cache-Control")).toBe("no-store");
        });
      }
    });
  }

  const FB_UA = "facebookexternalhit/1.1";
  const CHROME_UA =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36";

  for (const fixture of fixtures.og_entries) {
    it(`serves preview bots: ${fixture.name}`, async () => {
      await env.EDGE_CACHE.put(fixture.key, JSON.stringify(fixture.value));
      const code = fixture.key.split(":").slice(2).join(":");
      const response = await dispatch(
        new Request(`https://spoo.me/${encodeURIComponent(code)}`, {
          headers: { "user-agent": FB_UA },
        }),
      );

      expect(response.status).toBe(fixture.expect_preview.status);
      expect(await response.text()).toContain(
        fixture.expect_preview.body_contains,
      );
      expect(response.headers.get("Content-Type")).toContain("text/html");
      expect(response.headers.get("X-Spoo-Edge")).toBe("hit");
      expect(response.headers.get("X-Robots-Tag")).toContain("noindex");
    });

    it(`serves humans: ${fixture.name}`, async () => {
      await env.EDGE_CACHE.put(fixture.key, JSON.stringify(fixture.value));
      const code = fixture.key.split(":").slice(2).join(":");
      if (fixture.expect_human === "passthrough") {
        expectOriginFetch();
      }
      const response = await dispatch(
        new Request(`https://spoo.me/${encodeURIComponent(code)}`, {
          headers: { "user-agent": CHROME_UA },
        }),
      );

      if (fixture.expect_human === "passthrough") {
        expect(await response.text()).toBe("origin-served");
      } else {
        const expected = fixture.expect_human as {
          status: number;
          location: string;
        };
        expect(response.status).toBe(expected.status);
        expect(response.headers.get("Location")).toBe(expected.location);
      }
    });
  }

  it("?bot=1 bypasses the cache even when an og entry exists", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:ogdebug",
      JSON.stringify({ type: "og_only", status: 302, og_html: "<html></html>" }),
    );
    expectOriginFetch();
    const response = await dispatch(
      new Request("https://spoo.me/ogdebug?bot=1", {
        headers: { "user-agent": CHROME_UA },
      }),
    );
    expect(await response.text()).toBe("origin-served");
  });

  it("HEAD on an og entry gets the preview (200)", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:oghead",
      JSON.stringify({ type: "og_only", status: 302, og_html: "<html></html>" }),
    );
    const response = await dispatch(
      new Request("https://spoo.me/oghead", {
        method: "HEAD",
        headers: { "user-agent": CHROME_UA },
      }),
    );
    expect(response.status).toBe(200);
  });

  for (const fixture of fixtures.malformed) {
    it(`passes through: ${fixture.name}`, async () => {
      await env.EDGE_CACHE.put(fixture.key, fixture.raw);
      const code = fixture.key.split(":").slice(2).join(":");
      expectOriginFetch();
      const response = await dispatch(
        new Request(`https://spoo.me/${encodeURIComponent(code)}`),
      );

      expect(await response.text()).toBe("origin-served");
    });
  }
});

describe("serving behavior", () => {
  it("KV miss passes through to origin", async () => {
    expectOriginFetch("https://spoo.me", "/nothere");
    const response = await dispatch(new Request("https://spoo.me/nothere"));
    expect(await response.text()).toBe("origin-served");
    expect(response.headers.get("X-Spoo-Edge")).toBeNull();
  });

  it("HEAD is served from cache like GET", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:headcode",
      JSON.stringify({ type: "redirect", url: "https://example.com", status: 302 }),
    );
    const response = await dispatch(
      new Request("https://spoo.me/headcode", { method: "HEAD" }),
    );
    expect(response.status).toBe(302);
  });

  it("plain redirect entries never get Cache-Control", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:plaincc",
      JSON.stringify({ type: "redirect", url: "https://example.com", status: 302 }),
    );
    const response = await dispatch(
      geoRequest("https://spoo.me/plaincc", "IN"),
    );
    expect(response.status).toBe(302);
    expect(response.headers.get("Cache-Control")).toBeNull();
  });

  it("lowercase cf.country is normalized before rule lookup", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:geolc",
      JSON.stringify({
        type: "geo_redirect",
        url: "https://example.com/default",
        status: 302,
        rules: { IN: "https://example.in/" },
      }),
    );
    const response = await dispatch(geoRequest("https://spoo.me/geolc", "in"));
    expect(response.headers.get("Location")).toBe("https://example.in/");
  });

  it("www host is normalized to the canonical key", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:wwwcode",
      JSON.stringify({ type: "redirect", url: "https://example.com", status: 302 }),
    );
    const response = await dispatch(new Request("https://www.spoo.me/wwwcode"));
    expect(response.status).toBe(302);
  });

  it("?password= bypasses the cache even when an entry exists", async () => {
    await env.EDGE_CACHE.put(
      "cache:spoo.me:pwcode",
      JSON.stringify({ type: "redirect", url: "https://example.com", status: 302 }),
    );
    expectOriginFetch();
    const response = await dispatch(
      new Request("https://spoo.me/pwcode?password=hunter2"),
    );
    expect(await response.text()).toBe("origin-served");
  });

  it("POST passes through untouched", async () => {
    expectOriginFetch("https://spoo.me", "/", "POST");
    const response = await dispatch(
      new Request("https://spoo.me/", { method: "POST", body: "url=x" }),
    );
    expect(await response.text()).toBe("origin-served");
  });

  it("a throwing KV binding fails open to origin", async () => {
    expectOriginFetch("https://spoo.me", "/failopen");
    const brokenEnv = {
      ...env,
      EDGE_CACHE: {
        get: () => {
          throw new Error("kv exploded");
        },
      },
    } as unknown as typeof env;
    const ctx = createExecutionContext();
    const response = await worker.fetch(
      new Request("https://spoo.me/failopen") as IncomingRequest,
      brokenEnv,
      ctx,
    );
    await waitOnExecutionContext(ctx);
    expect(await response.text()).toBe("origin-served");
  });
});

describe("lookupKey routing", () => {
  const cases: Array<[string, string | null]> = [
    ["https://spoo.me/abc1234", "cache:spoo.me:abc1234"],
    ["https://www.spoo.me/abc1234", "cache:spoo.me:abc1234"],
    ["https://spoo.me/%F0%9F%9A%80", "cache:spoo.me:🚀"],
    ["https://spoo.me/", null],
    ["https://spoo.me/api/v1/shorten", null],
    ["https://spoo.me/dashboard/urls", null],
    ["https://spoo.me/auth/login", null],
    ["https://spoo.me/oauth/google", null],
    ["https://spoo.me/static/app.css", null],
    ["https://spoo.me/stats/abc1234", null],
    ["https://spoo.me/favicon.ico", null],
    ["https://spoo.me/a/b", null],
    ["https://spoo.me/abc?password=x", null],
    // Next-owned single-segment pages: reserved aliases, exact-match skip.
    ["https://spoo.me/pricing", null],
    ["https://spoo.me/onboarding", null],
    ["https://spoo.me/relay/e", null],
    // ...but a reserved-adjacent alias keeps its lookup.
    ["https://spoo.me/aboutus", "cache:spoo.me:aboutus"],
    // /{code}+ is preview intent, never a key (emoji arrives encoded).
    ["https://spoo.me/abc1234+", null],
    ["https://spoo.me/%F0%9F%9A%80+", null],
  ];

  for (const [url, expected] of cases) {
    it(`${url} → ${expected}`, () => {
      expect(lookupKey(new Request(url))).toBe(expected);
    });
  }

  it("non-GET/HEAD methods never produce a key", () => {
    expect(
      lookupKey(new Request("https://spoo.me/abc", { method: "POST" })),
    ).toBeNull();
  });
});
