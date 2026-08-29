"""Tests for the destination meta-tag parser."""

from __future__ import annotations

from services.meta_tags.parse_html import parse_meta_tags

BASE = "https://dest.example.com/article/42"


def test_full_og_page():
    html = """<!doctype html><html><head>
    <title>HTML Title</title>
    <meta property="og:title" content="OG Title">
    <meta property="og:description" content="OG Desc">
    <meta property="og:image" content="https://dest.example.com/og.png">
    <meta property="og:site_name" content="Dest">
    <meta name="twitter:title" content="TW Title">
    <meta name="theme-color" content="#FF5733">
    </head><body>ignored</body></html>"""
    p = parse_meta_tags(html, BASE)
    assert p.title == "OG Title"  # og beats twitter beats <title>
    assert p.description == "OG Desc"
    assert p.image == "https://dest.example.com/og.png"
    assert p.color == "#FF5733"
    assert p.site_name == "Dest"
    assert p.og["title"] == "OG Title"
    assert p.twitter["title"] == "TW Title"


def test_fallback_chain_title():
    html = "<head><title>Only Title</title></head>"
    assert parse_meta_tags(html, BASE).title == "Only Title"
    html = '<head><meta name="twitter:title" content="TW"><title>T</title></head>'
    assert parse_meta_tags(html, BASE).title == "TW"


def test_property_name_attribute_mixups_accepted():
    # Sites swap property=/name= constantly — accept both for both families.
    html = """<head>
    <meta name="og:title" content="via name">
    <meta property="twitter:description" content="via property">
    </head>"""
    p = parse_meta_tags(html, BASE)
    assert p.title == "via name"
    assert p.description == "via property"


def test_first_tag_wins():
    html = """<head>
    <meta property="og:title" content="first">
    <meta property="og:title" content="second">
    </head>"""
    assert parse_meta_tags(html, BASE).title == "first"


def test_relative_image_resolved_against_final_url():
    html = '<head><meta property="og:image" content="/img/og.png"></head>'
    assert parse_meta_tags(html, BASE).image == "https://dest.example.com/img/og.png"


def test_http_image_dropped():
    html = '<head><meta property="og:image" content="http://insecure/og.png"></head>'
    assert parse_meta_tags(html, BASE).image is None


def test_bad_theme_color_dropped():
    html = '<head><meta name="theme-color" content="rebeccapurple"></head>'
    assert parse_meta_tags(html, BASE).color is None


def test_body_meta_ignored():
    # Stop at <body>: preview crawlers only read head, and so do we.
    html = """<head><title>T</title></head>
    <body><meta property="og:title" content="sneaky"></body>"""
    p = parse_meta_tags(html, BASE)
    assert p.title == "T"


def test_broken_html_tolerated():
    html = '<head><meta property="og:title" content="ok"><div><span></head>'
    assert parse_meta_tags(html, BASE).title == "ok"


def test_entities_decoded():
    html = '<head><meta property="og:title" content="A &amp; B"></head>'
    assert parse_meta_tags(html, BASE).title == "A & B"


def test_no_tags_page():
    p = parse_meta_tags("<html><head></head><body></body></html>", BASE)
    assert p.title is None and p.description is None and p.image is None


def test_html_title_and_description_exposed_raw():
    # The audit surface needs the raw <title>/<meta description> even when
    # og tags exist and win the normalized picks.
    html = """<head>
    <title>HTML Title</title>
    <meta name="description" content="HTML Desc">
    <meta property="og:title" content="OG Title">
    <meta property="og:description" content="OG Desc">
    </head>"""
    p = parse_meta_tags(html, BASE)
    assert p.title == "OG Title"
    assert p.html_title == "HTML Title"
    assert p.html_description == "HTML Desc"


def test_favicon_prefers_apple_touch_icon():
    html = """<head>
    <link rel="icon" href="/favicon.ico" sizes="16x16">
    <link rel="apple-touch-icon" href="/apple-icon.png">
    <link rel="shortcut icon" href="/legacy.ico">
    </head>"""
    p = parse_meta_tags(html, BASE)
    assert p.favicon == "https://dest.example.com/apple-icon.png"


def test_favicon_picks_largest_declared_size():
    html = """<head>
    <link rel="icon" href="/small.png" sizes="16x16">
    <link rel="icon" href="/big.png" sizes="64x64">
    </head>"""
    assert parse_meta_tags(html, BASE).favicon == "https://dest.example.com/big.png"


def test_favicon_defaults_to_convention_path():
    p = parse_meta_tags("<head><title>T</title></head>", BASE)
    assert p.favicon == "https://dest.example.com/favicon.ico"
