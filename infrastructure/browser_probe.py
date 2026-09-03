"""Own-browser probe client: the render egress that also CLICKS.

The probe service (``browser/probe.py``) loads a page in our own Chromium,
screenshots it, clicks its most prominent controls and records what each
click caused. This client turns that record into plain observations for
the investigator's model, so "what a click does" is something the model
READ, never something it inferred from script tags.

Failures return None: the caller falls back to the one-shot Cloudflare
snapshot, which sees the page but cannot touch it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger
from shared.url_utils import registrable_domain

log = get_logger(__name__)

EGRESS_LABEL = "our own browser (Hetzner datacenter IP), scripted probe"


@dataclass(frozen=True)
class ProbeResult:
    url: str
    final_url: str
    html: str
    screenshot: bytes
    screenshot_after: bytes
    media_type: str
    observations: str
    egress: str = EGRESS_LABEL


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""


def _cross(a: str, b: str) -> str:
    return (
        " [cross-domain]"
        if registrable_domain(_host(a)) != registrable_domain(_host(b))
        else ""
    )


def format_observations(body: dict) -> str:
    """The probe record as facts, one line per thing that happened."""
    url = body.get("url", "")
    landed = body.get("landed_url") or body.get("final_url") or url
    hops = body.get("hops") or [url]
    lines = [
        "A scripted probe loaded the page, then clicked its most prominent "
        "controls. This is what HAPPENED, not what the markup suggests:"
    ]
    loaded = (
        f"{' → '.join(hops)} ({len(hops) - 1} HTTP redirect hops)"
        if len(hops) > 1
        else hops[0]
    )
    if landed.rstrip("/") != hops[-1].rstrip("/"):
        loaded += (
            f" → landed on {landed}{_cross(hops[-1], landed)} (JS or meta refresh)"
        )
    lines.append(f"loaded: {loaded}")
    autos = body.get("auto_redirects") or []
    if autos:
        for target in autos:
            lines.append(
                f"after load: redirected on its own to {target}{_cross(landed, target)}"
            )
    else:
        lines.append("after load: stayed on the page, no automatic redirect")
    clicks = body.get("clicks") or []
    if not clicks:
        lines.append("clicks: no visible clickable control found")
    for i, c in enumerate(clicks, start=1):
        effects = []
        for p in c.get("popups") or []:
            effects.append(f"opened pop-up {p}{_cross(landed, p)}")
        nav = c.get("navigated_to")
        if nav:
            effects.append(f"navigated to {nav}{_cross(landed, nav)}")
        for d in c.get("downloads") or []:
            effects.append(f"started download {d}")
        for d in c.get("dialogs") or []:
            effects.append(f"dialog {d}")
        rev = c.get("revealed") or {}
        if rev.get("text"):
            effects.append(
                "revealed on the page: " + " | ".join(repr(t[:80]) for t in rev["text"])
            )
        if rev.get("inputs"):
            effects.append("new form fields: " + ", ".join(rev["inputs"]))
        if rev.get("frames"):
            effects.append(
                "new frames from: "
                + ", ".join(_host(f) or f[:60] for f in rev["frames"])
            )
        for kind, detail in c.get("events") or []:
            if kind == "clipboard_write":
                effects.append(f"WROTE TO CLIPBOARD: {detail!r}")
            elif kind == "notification_prompt":
                effects.append("asked for notification permission")
            elif kind == "click_failed":
                effects.append("click failed")
        if not effects and not nav:
            effects.append("nothing observable, stayed on page")
        lines.append(f"click {i}: {c.get('target', '?')} → {'; '.join(effects)}")
    clip = [d for k, d in body.get("events") or [] if k == "clipboard_write"]
    lines.append(
        "clipboard writes: " + (", ".join(repr(c) for c in clip) if clip else "none")
    )
    prompted = any(k == "notification_prompt" for k, _ in body.get("events") or [])
    lines.append(
        "notification permission prompt: "
        + ("requested" if prompted else "not requested")
    )
    downloads = body.get("downloads") or []
    lines.append("downloads: " + (", ".join(downloads) if downloads else "none"))
    dialogs = body.get("dialogs") or []
    lines.append("dialogs: " + (", ".join(dialogs) if dialogs else "none"))
    failed = [d for k, d in body.get("events") or [] if k == "candidates_failed"]
    if failed:
        lines.append(f"probe note: could not enumerate controls ({failed[0]})")
    blocked = body.get("blocked_requests") or []
    if blocked:
        lines.append(f"requests to private addresses refused: {len(blocked)}")
    lines.append(f"final page: {body.get('final_url') or landed}")
    return "\n".join(lines)


class BrowserProbeClient:
    def __init__(
        self, http_client: HttpClient, *, base_url: str, timeout_seconds: float = 75.0
    ) -> None:
        self._http = http_client
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._base)

    async def probe(self, url: str) -> ProbeResult | None:
        if not self.configured:
            return None
        try:
            response = await self._http.post(
                f"{self._base}/probe", json={"url": url}, timeout=self._timeout
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            log.warning(
                "browser_probe_failed",
                url=url,
                error=str(exc)[:300],
                error_type=type(exc).__name__,
            )
            return None
        html = body.get("html") or ""
        shot = _b64(body.get("screenshot"))
        if not html and not shot:
            log.warning("browser_probe_empty", url=url)
            return None
        return ProbeResult(
            url=url,
            final_url=body.get("final_url") or url,
            html=html,
            screenshot=shot,
            screenshot_after=_b64(body.get("screenshot_after")),
            media_type=body.get("screenshot_type") or "image/jpeg",
            observations=format_observations(body),
        )


def _b64(value: str | None) -> bytes:
    if not value:
        return b""
    try:
        return base64.b64decode(value)
    except Exception:
        return b""
