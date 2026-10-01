"""Tests for GET /api/v1/emoji-set.

Public, unauthenticated read of the emoji-alias acceptance policy. The
endpoint reads only settings and the pure ``shared.emoji_policy``
derivation, so no repo mocks are needed — the real AppSettings from the
test lifespan drives it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import AppSettings
from app.shared.emoji_policy import accepted_singletons, check_emoji_alias

from .conftest import _build_test_app

# The canonical Unicode emoji groups, in order — every emoji picker's tabs.
CANONICAL_GROUPS = [
    "Smileys & Emotion",
    "People & Body",
    "Component",
    "Animals & Nature",
    "Food & Drink",
    "Travel & Places",
    "Activities",
    "Objects",
    "Symbols",
    "Flags",
]

# Known-safe single-codepoint emoji.
STAR = "⭐"
PARTY = "\U0001f389"  # 🎉
GRINNING = "\U0001f600"  # 😀 first in canonical order (Smileys & Emotion)
ROCKET = "\U0001f680"  # 🚀 Travel & Places
RED_CIRCLE = "\U0001f534"  # 🔴 Symbols — sorts far after any smiley
# Known-rejected forms.
US_FLAG = "\U0001f1fa\U0001f1f8"  # 🇺🇸 regional-indicator flag
WOMAN_TECHNOLOGIST = "\U0001f469‍\U0001f4bb"  # 👩‍💻 ZWJ family
KEYCAP_ONE = "1️⃣"  # 1️⃣ keycap
SMILEY_TEXT_DEFAULT = "☺"  # text-default without VS16


def _get():
    application = _build_test_app({})
    with TestClient(application, raise_server_exceptions=False) as client:
        return client.get("/api/v1/emoji-set")


def test_returns_200_unauthenticated():
    resp = _get()
    assert resp.status_code == 200


def test_response_shape():
    body = _get().json()
    assert set(body) == {
        "accept_max_version",
        "generate_max_version",
        "max_graphemes",
        "emoji",
    }
    emoji_list = body["emoji"]
    assert isinstance(emoji_list, list)
    assert len(emoji_list) > 0
    # Every entry is a {c, n, g, gen} object (k is optional).
    for entry in emoji_list:
        assert set(entry) <= {"c", "n", "g", "gen", "k"}
        assert {"c", "n", "g", "gen"} <= set(entry)
        assert isinstance(entry["c"], str) and entry["c"]
        assert isinstance(entry["n"], str)
        assert isinstance(entry["g"], str) and entry["g"]
        assert isinstance(entry["gen"], bool)


def test_names_are_non_colon_non_empty():
    for entry in _get().json()["emoji"]:
        assert entry["n"]
        assert ":" not in entry["n"]


def test_optional_keywords_when_present_is_a_string_list():
    for entry in _get().json()["emoji"]:
        if "k" in entry:
            assert isinstance(entry["k"], list) and entry["k"]
            assert all(isinstance(x, str) and x for x in entry["k"])


def test_caps_and_max_graphemes_match_settings():
    settings = AppSettings()
    body = _get().json()
    assert body["accept_max_version"] == settings.emoji_accept_max_version
    assert body["generate_max_version"] == settings.emoji_generate_max_version
    assert body["max_graphemes"] == settings.max_emoji_alias_length


def test_contains_known_safe_emoji_with_expected_name():
    by_char = {e["c"]: e for e in _get().json()["emoji"]}
    assert STAR in by_char and by_char[STAR]["n"] == "star"
    assert PARTY in by_char and by_char[PARTY]["n"] == "party popper"
    # 🚀 rocket resolves by name — the search use case.
    assert any(e["n"] == "rocket" and e["c"] == "\U0001f680" for e in by_char.values())


def test_every_entry_has_a_canonical_group():
    for entry in _get().json()["emoji"]:
        assert entry["g"] in CANONICAL_GROUPS


def test_known_emoji_map_to_expected_groups():
    by_char = {e["c"]: e for e in _get().json()["emoji"]}
    assert by_char[GRINNING]["g"] == "Smileys & Emotion"
    assert by_char[ROCKET]["g"] == "Travel & Places"
    assert by_char[RED_CIRCLE]["g"] == "Symbols"


def test_array_is_in_canonical_group_order():
    emoji_list = _get().json()["emoji"]
    # Opens on Smileys, not medals/ATM/symbols: the first item is a smiley.
    assert emoji_list[0]["g"] == "Smileys & Emotion"
    # Groups appear as contiguous runs in canonical order — the run of group
    # indices is non-decreasing across the whole array.
    order = {name: i for i, name in enumerate(CANONICAL_GROUPS)}
    indices = [order[e["g"]] for e in emoji_list]
    assert indices == sorted(indices)
    # A smiley sorts before a Symbols-group glyph (medals/AB/ATM live there).
    chars = [e["c"] for e in emoji_list]
    assert chars.index(GRINNING) < chars.index(RED_CIRCLE)


def test_grouping_does_not_change_the_accepted_set():
    # THE invariant: adding g + reordering is a pure permutation of the
    # policy-derived accepted set — same chars, same count, no dupes.
    chars = [e["c"] for e in _get().json()["emoji"]]
    accepted = accepted_singletons(AppSettings().emoji_accept_max_version)
    assert len(chars) == len(set(chars)) == len(accepted)
    assert set(chars) == set(accepted)


def test_gen_flagged_entries_are_within_the_safe_space():
    generate = [e["c"] for e in _get().json()["emoji"] if e["gen"]]
    assert len(generate) > 0
    # Every gen=true entry is itself an accepted single-codepoint alias.
    for e in generate:
        assert check_emoji_alias(e) == "ok"


def test_excludes_known_rejected_forms():
    chars = {e["c"] for e in _get().json()["emoji"]}
    assert US_FLAG not in chars  # regional-indicator flag
    assert WOMAN_TECHNOLOGIST not in chars  # ZWJ family
    assert KEYCAP_ONE not in chars  # keycap
    assert SMILEY_TEXT_DEFAULT not in chars  # text-default, needs VS16


def test_excludes_anything_the_policy_would_reject():
    # Nothing the picker offers may be something the create endpoint 400s:
    # the derivation agrees with the validator for the whole sample.
    accept_cap = AppSettings().emoji_accept_max_version
    for entry in _get().json()["emoji"]:
        assert check_emoji_alias(entry["c"], max_version=accept_cap) == "ok"


def test_cache_control_header():
    resp = _get()
    assert resp.headers["cache-control"] == (
        "public, max-age=86400, stale-while-revalidate=604800"
    )


def test_etag_present_and_stable():
    first, second = _get(), _get()
    assert first.headers.get("etag")
    assert first.headers["etag"] == second.headers["etag"]


def test_conditional_request_returns_304():
    etag = _get().headers["etag"]
    application = _build_test_app({})
    with TestClient(application, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/emoji-set", headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert not resp.content


def test_weak_if_none_match_returns_304():
    # Behind Cloudflare a strong ETag is weakened to ``W/"..."`` on compressed
    # responses, so the browser echoes the weak form back. RFC 9110's weak
    # comparison must still yield a 304.
    etag = _get().headers["etag"]
    application = _build_test_app({})
    with TestClient(application, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/emoji-set", headers={"If-None-Match": f"W/{etag}"})
    assert resp.status_code == 304
    assert not resp.content


def test_wildcard_if_none_match_returns_304():
    application = _build_test_app({})
    with TestClient(application, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/emoji-set", headers={"If-None-Match": "*"})
    assert resp.status_code == 304
    assert not resp.content
