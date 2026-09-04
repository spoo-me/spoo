"""Scripted page probe: what a page DOES, not just what it shows.

One Chromium, one context per probe. Load the URL, wait for the redirects
a plain HTTP client cannot see, screenshot, then click the page's most
prominent controls and record what each click caused: pop-ups, navigations,
downloads, dialogs, clipboard writes, notification prompts. Nothing here
judges; the investigator's model reads the record.

The browser sits on its own docker network and every request it makes is
checked against the resolved address, so a hostile page cannot use it to
reach anything private.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MAX_CLICKS = int(os.environ.get("PROBE_MAX_CLICKS", "3"))
GOTO_TIMEOUT_MS = int(os.environ.get("PROBE_GOTO_TIMEOUT_MS", "20000"))
SETTLE_MS = int(os.environ.get("PROBE_SETTLE_MS", "2500"))
CLICK_WAIT_MS = int(os.environ.get("PROBE_CLICK_WAIT_MS", "2500"))
PROBE_BUDGET_S = float(os.environ.get("PROBE_BUDGET_SECONDS", "60"))
CONCURRENCY = int(os.environ.get("PROBE_CONCURRENCY", "2"))
HTML_CAP = 400_000
MAX_EVENTS = 100
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_INIT_SCRIPT = """
(() => {
  const rec = (k, v) => { try { window.__probeRec(k, String(v == null ? '' : v).slice(0, 400)); } catch (e) {} };
  if (window.Notification && Notification.requestPermission) {
    const orig = Notification.requestPermission.bind(Notification);
    Notification.requestPermission = function () { rec('notification_prompt', location.href); return orig.apply(this, arguments); };
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    const w = navigator.clipboard.writeText.bind(navigator.clipboard);
    navigator.clipboard.writeText = (t) => { rec('clipboard_write', t); return w(t); };
  }
  const ec = document.execCommand.bind(document);
  document.execCommand = function (cmd) {
    if (String(cmd).toLowerCase() === 'copy') {
      const sel = (window.getSelection() || '').toString();
      const ae = document.activeElement;
      rec('clipboard_write', sel || (ae && ae.value) || '');
    }
    return ec.apply(document, arguments);
  };
  const wo = window.open;
  window.open = function (u) { rec('window_open', u || ''); return wo.apply(window, arguments); };
  if (navigator.serviceWorker && navigator.serviceWorker.register) {
    const r = navigator.serviceWorker.register.bind(navigator.serviceWorker);
    navigator.serviceWorker.register = (s, o) => { rec('service_worker', s); return r(s, o); };
  }
})();
"""

_CANDIDATES_JS = """
(args) => {
  const [max, skip] = args;
  const KW = /play|watch|stream|continue|verify|verif|copy|download|allow|start|claim|next|get\\b|open|unlock|install|login|sign|confirm|skip|proceed|human|robot|captcha|enter|accept|lanjut|klik|mulai|tonton/i;
  const vw = innerWidth, vh = innerHeight;
  const seen = new Set(), out = [];
  const els = document.querySelectorAll('button, a[href], [role=button], input[type=submit], input[type=button], input[type=image], video, [onclick], summary, label, div, span, img, svg');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 16) continue;
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.05) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    const clickable = ['button', 'a', 'video', 'summary', 'label', 'input'].includes(tag) || role === 'button' || el.hasAttribute('onclick') || cs.cursor === 'pointer';
    if (!clickable) continue;
    const text = ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '') + ' ' + (typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '')).replace(/\\s+/g, ' ').trim().slice(0, 120);
    const cx = r.left + Math.min(r.width, vw - r.left) / 2, cy = r.top + Math.min(r.height, vh - r.top) / 2;
    const top = document.elementFromPoint(cx, cy);
    if (!top || !(el === top || el.contains(top) || top.contains(el))) continue;
    const key = tag + '|' + text;
    if (skip.includes(key)) continue;
    const pos = Math.round(cx) + ',' + Math.round(cy);
    if (seen.has(pos)) continue;
    seen.add(pos);
    const area = Math.min(r.width * r.height, vw * vh) / (vw * vh);
    const score = (KW.test(text) ? 10 : 0) + (['button', 'a', 'video'].includes(tag) || role === 'button' ? 3 : 0) + (tag === 'video' ? 4 : 0) + area * 5;
    out.push({ x: cx, y: cy, tag, text, key, href: el.href || null, score });
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, max);
}
"""

_DOC_JS = """
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const inputs = [], frames = [], text = [];
  if (document.body) text.push(...document.body.innerText.split('\\n'));
  const walk = (root, depth) => {
    if (depth > 6) return;
    for (const e of root.querySelectorAll('*')) {
      const tag = e.tagName.toLowerCase();
      if ((tag === 'input' || tag === 'select' || tag === 'textarea') && vis(e) && inputs.length < 40)
        inputs.push((e.getAttribute('type') || tag) + ':' + (e.name || e.id || e.placeholder || e.getAttribute('aria-label') || e.getAttribute('autocomplete') || ''));
      if (tag === 'iframe' && e.src && frames.length < 20) frames.push(e.src);
      if (e.shadowRoot) {
        for (const c of e.shadowRoot.children) { if (c.innerText) text.push(...c.innerText.split('\\n')); }
        walk(e.shadowRoot, depth + 1);
      }
    }
  };
  walk(document, 0);
  return { inputs, frames, text: text.map(t => t.trim()).filter(t => t.length > 2).slice(0, 400) };
}
"""


def _log(event: str, **kw) -> None:
    sys.stdout.write(json.dumps({"event": event, **kw}, default=str) + "\n")
    sys.stdout.flush()


def _is_public_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.hostname)


class _Guard:
    """Refuse any request whose host resolves to a non-public address."""

    def __init__(self) -> None:
        self._cache: dict[str, bool] = {}

    async def public(self, host: str) -> bool:
        if host in self._cache:
            return self._cache[host]
        try:
            ipaddress.ip_address(host)
            ok = ipaddress.ip_address(host).is_global
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, None)
            except OSError:
                ok = False
            else:
                addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
                ok = bool(addrs) and all(a.is_global for a in addrs)
        self._cache[host] = ok
        return ok


@dataclass
class _Click:
    target: str
    href: str | None
    popups: list[str] = field(default_factory=list)
    navigated_to: str | None = None
    downloads: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    events: list[list[str]] = field(default_factory=list)
    revealed: dict = field(default_factory=dict)


def _revealed(before: dict, after: dict) -> dict:
    """What appeared on the page after a click: a modal's text, fresh form
    fields, injected frames. A card-harvesting overlay is none of navigation,
    pop-up or download, and this is where it shows."""
    seen = set(before.get("text") or [])
    out = {
        "text": [t for t in after.get("text") or [] if t not in seen][:8],
        "inputs": [
            i
            for i in after.get("inputs") or []
            if i not in (before.get("inputs") or [])
        ][:12],
        "frames": [
            f
            for f in after.get("frames") or []
            if f not in (before.get("frames") or [])
        ][:6],
    }
    return {k: v for k, v in out.items() if v}


@dataclass
class _Record:
    start: float
    hops: list[str] = field(default_factory=list)
    auto_redirects: list[str] = field(default_factory=list)
    clicks: list[_Click] = field(default_factory=list)
    popups: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    events: list[list[str]] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    tasks: list = field(default_factory=list)
    current: _Click | None = None

    def event(self, kind: str, detail: str) -> None:
        # A page can call the binding in a loop; the record stays bounded.
        if len(self.events) >= MAX_EVENTS:
            if self.events[-1][0] != "truncated":
                self.events.append(["truncated", f"more than {MAX_EVENTS} events"])
            return
        item = [kind, detail[:400]]
        self.events.append(item)
        if self.current is not None:
            self.current.events.append(item)


class Prober:
    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._sem = asyncio.Semaphore(CONCURRENCY)
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        await self._launch()

    async def _launch(self) -> None:
        self._browser = await self._pw.chromium.launch(
            args=["--disable-dev-shm-usage", "--disable-gpu", "--no-first-run"]
        )
        _log("browser_launched", version=self._browser.version)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _browser_ready(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                _log("browser_relaunch")
                await self._launch()
            return self._browser

    async def probe(self, url: str) -> dict:
        async with self._sem:
            browser = await self._browser_ready()
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=USER_AGENT,
                locale="en-US",
                ignore_https_errors=True,
                accept_downloads=True,
                service_workers="block",
            )
            try:
                return await asyncio.wait_for(
                    self._run(context, url), timeout=PROBE_BUDGET_S
                )
            finally:
                await context.close()

    async def _run(self, context: BrowserContext, url: str) -> dict:
        rec = _Record(start=time.monotonic())
        guard = _Guard()

        async def route(r, request):
            host = urlparse(request.url).hostname or ""
            if not _is_public_url(request.url) or not await guard.public(host):
                rec.blocked.append(request.url[:200])
                await r.abort()
                return
            await r.continue_()

        await context.route("**/*", route)
        await context.expose_binding(
            "__probeRec", lambda source, k, v: rec.event(str(k), str(v))
        )
        await context.add_init_script(_INIT_SCRIPT)

        page = await context.new_page()

        def on_new_page(p: Page) -> None:
            async def settle() -> None:
                with contextlib.suppress(Exception):
                    await p.wait_for_load_state("domcontentloaded", timeout=4000)
                target = p.url
                rec.popups.append(target)
                if rec.current is not None:
                    rec.current.popups.append(target)
                with contextlib.suppress(Exception):
                    await p.close()

            rec.tasks.append(asyncio.ensure_future(settle()))

        def on_dialog(d) -> None:
            msg = f"{d.type}: {d.message}"[:300]
            rec.dialogs.append(msg)
            if rec.current is not None:
                rec.current.dialogs.append(msg)
            rec.tasks.append(asyncio.ensure_future(d.dismiss()))

        def on_download(dl) -> None:
            desc = f"{dl.suggested_filename} from {dl.url}"[:300]
            rec.downloads.append(desc)
            if rec.current is not None:
                rec.current.downloads.append(desc)
            rec.tasks.append(asyncio.ensure_future(dl.cancel()))

        context.on("page", on_new_page)
        page.on("dialog", on_dialog)
        page.on("download", on_download)

        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            # A page that never fires DOMContentLoaded (endless beacons) can
            # still have committed a document worth looking at.
            rec.event(
                "goto_slow", "domcontentloaded timed out, using committed document"
            )
            response = await page.goto(
                url, wait_until="commit", timeout=GOTO_TIMEOUT_MS
            )
        if response is not None:
            req = response.request
            chain = []
            while req is not None:
                chain.append(req.url)
                req = req.redirected_from
            rec.hops = list(reversed(chain))
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=5000)
        landed = page.url
        await page.wait_for_timeout(SETTLE_MS)
        if page.url != landed:
            rec.auto_redirects.append(page.url)

        title = await _safe_title(page)
        html = (await _safe_content(page))[:HTML_CAP]
        shot_before = await _safe_shot(page)

        skip: list[str] = []
        clicks_left, scrolls_left = MAX_CLICKS, 2
        while clicks_left:
            try:
                cands = await page.evaluate(_CANDIDATES_JS, [1, skip])
            except Exception as exc:
                rec.event("candidates_failed", f"{type(exc).__name__}: {exc}")
                break
            if not cands:
                if not scrolls_left:
                    break
                scrolls_left -= 1
                await page.mouse.wheel(0, 700)
                await page.wait_for_timeout(500)
                continue
            clicks_left -= 1
            c = cands[0]
            skip.append(c["key"])
            click = _Click(target=f"{c['tag']} '{c['text']}'", href=c.get("href"))
            rec.current = click
            rec.clicks.append(click)
            before_url = page.url
            try:
                before_doc = await page.evaluate(_DOC_JS)
            except Exception as exc:
                rec.event("doc_diff_failed", f"{type(exc).__name__}: {exc}")
                before_doc = {}
            try:
                await page.mouse.click(c["x"], c["y"])
            except Exception as exc:
                click.events.append(["click_failed", type(exc).__name__])
                rec.current = None
                continue
            await page.wait_for_timeout(CLICK_WAIT_MS)
            if click.popups:
                await page.wait_for_timeout(1000)
            if page.url != before_url:
                click.navigated_to = page.url
                rec.current = None
                break
            try:
                after_doc = await page.evaluate(_DOC_JS)
            except Exception as exc:
                rec.event("doc_diff_failed", f"{type(exc).__name__}: {exc}")
            else:
                click.revealed = _revealed(before_doc, after_doc)
            rec.current = None

        shot_after = await _safe_shot(page)
        if shot_after == shot_before:
            shot_after = b""

        return {
            "url": url,
            "final_url": page.url,
            "landed_url": landed,
            "title": title,
            "html": html,
            "screenshot": base64.b64encode(shot_before).decode() if shot_before else "",
            "screenshot_after": base64.b64encode(shot_after).decode()
            if shot_after
            else "",
            "screenshot_type": "image/jpeg",
            "hops": rec.hops,
            "auto_redirects": rec.auto_redirects,
            "clicks": [c.__dict__ for c in rec.clicks],
            "popups": rec.popups,
            "downloads": rec.downloads,
            "dialogs": rec.dialogs,
            "events": rec.events,
            "blocked_requests": rec.blocked[:20],
            "elapsed_ms": int((time.monotonic() - rec.start) * 1000),
        }


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def _safe_content(page: Page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


async def _safe_shot(page: Page) -> bytes:
    try:
        return await page.screenshot(type="jpeg", quality=60, timeout=8000)
    except Exception:
        return b""


prober = Prober()


async def health(_: Request) -> JSONResponse:
    ok = prober._browser is not None and prober._browser.is_connected()
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


async def probe(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    url = str(body.get("url", ""))
    if not _is_public_url(url):
        return JSONResponse({"error": "http/https URL required"}, status_code=400)
    started = time.monotonic()
    try:
        result = await prober.probe(url)
    except asyncio.TimeoutError:
        _log("probe_timeout", url=url)
        return JSONResponse({"error": "probe budget exceeded"}, status_code=504)
    except Exception as exc:
        _log(
            "probe_failed", url=url, error=str(exc)[:300], error_type=type(exc).__name__
        )
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, status_code=502
        )
    _log(
        "probe_done",
        url=url,
        final_url=result["final_url"],
        clicks=len(result["clicks"]),
        popups=len(result["popups"]),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return JSONResponse(result)


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    await prober.start()
    try:
        yield
    finally:
        await prober.stop()


app = Starlette(
    routes=[Route("/health", health), Route("/probe", probe, methods=["POST"])],
    lifespan=lifespan,
)
