"""
Long-form OpenAPI descriptions for query parameter fields.

Extracted here to keep Pydantic model definitions concise while still
producing rich, detailed API documentation via FastAPI's auto-generated
OpenAPI spec.  Each constant is imported into the relevant model's
``Field(description=...)`` argument.
"""

# ── StatsQuery / ExportQuery ─────────────────────────────────────────────────

STATS_SCOPE_DESC = (
    "Statistics scope: `all` (authenticated only) or `anon` (public access).\n\n"
    "- `all` — aggregate stats across all URLs owned by the authenticated user. "
    "Requires authentication.\n"
    "- `anon` — public stats for a single URL. Requires `short_code` parameter. "
    "No authentication needed (unless stats are private)."
)

STATS_SHORT_CODE_DESC = (
    "URL alias to query stats for. **Required** when `scope=anon`. "
    "When `scope=all`, this is optional and filters stats to a specific URL."
)

STATS_START_DATE_DESC = (
    "Start of time range. Accepts ISO 8601 datetime string "
    "(e.g., `2025-01-01T00:00:00Z`) or Unix timestamp in seconds "
    "(e.g., `1735689600`). If omitted, defaults to the URL creation date."
)

STATS_END_DATE_DESC = (
    "End of time range. Accepts ISO 8601 datetime string "
    "(e.g., `2025-12-31T23:59:59Z`) or Unix timestamp in seconds "
    "(e.g., `1767225599`). If omitted, defaults to now."
)

STATS_GROUP_BY_DESC = (
    "Comma-separated grouping dimensions for the statistics breakdown. "
    "Defaults to `time` if omitted.\n\n"
    "**Available dimensions:**\n\n"
    "- `time` — group by time buckets (day/week/month, auto-selected based on range)\n"
    "- `browser` — group by browser name (e.g., Chrome, Firefox, Safari)\n"
    "- `os` — group by operating system (e.g., Windows, macOS, Linux)\n"
    "- `device` — group by device type (`mobile`, `tablet`, `desktop`, `unknown`)\n"
    "- `country` — group by country\n"
    "- `city` — group by city\n"
    "- `referrer` — group by referrer URL\n"
    "- `short_code` — group by URL alias (only with `scope=all`)\n"
    "- `domain` — group by the domain the short link is served on "
    "(clicks recorded before domain tracking appear as `unknown`)\n"
    "- `utm_source` — group by the `utm_source` tag on the short link "
    "(untagged clicks appear as `(none)`)\n"
    "- `utm_medium` — group by the `utm_medium` tag\n"
    "- `utm_campaign` — group by the `utm_campaign` tag\n\n"
    "Multiple dimensions can be combined: `time,browser` returns time series "
    "broken down by browser."
)

STATS_METRICS_DESC = (
    "Comma-separated metrics to include. Defaults to `clicks,unique_clicks` "
    "if omitted.\n\n"
    "**Available metrics:**\n\n"
    "- `clicks` — total click count\n"
    "- `unique_clicks` — unique visitor count (deduplicated by IP + User-Agent)"
)

STATS_TIMEZONE_DESC = (
    "IANA timezone name for time-based grouping and output formatting "
    "(e.g., `UTC`, `America/New_York`, `Asia/Kolkata`). Defaults to `UTC`."
)

STATS_FILTERS_DESC = (
    "**Method 1: JSON Filters Object**\n\n"
    "JSON string containing dimension filters. "
    'Format: `{"dimension": ["value1", "value2"]}`\n\n'
    "**Available filter dimensions:**\n\n"
    "- `browser` — Filter by browser name (e.g., Chrome, Firefox, Safari, Edge)\n"
    "- `os` — Filter by operating system (e.g., Windows, macOS, Linux, iOS, Android)\n"
    "- `device` — Filter by device type (`mobile`, `tablet`, `desktop`, `unknown`)\n"
    "- `country` — Filter by country name (e.g., United States, Canada, Germany)\n"
    "- `city` — Filter by city name (e.g., New York, London, Mumbai)\n"
    "- `referrer` — Filter by referrer URL (e.g., https://google.com, https://twitter.com)\n"
    "- `short_code` — Filter by URL alias (e.g., mylink, promo2024) — "
    "**not allowed** with `scope=anon`\n"
    "- `domain` — Filter by the serving domain (e.g., spoo.me, "
    "links.example.com); `unknown` matches clicks recorded before domain "
    "tracking existed\n"
    "- `utm_source` / `utm_medium` / `utm_campaign` — Filter by campaign tags; "
    "`(none)` matches untagged clicks\n\n"
    "**Value format:** Array of strings for each dimension.\n\n"
    "**Important:** Filter values are case-sensitive. Use exact capitalization "
    "as stored in the database.\n\n"
    "**Examples:**\n\n"
    '- `{"browser": ["Chrome", "Firefox"]}` — Chrome OR Firefox clicks\n'
    '- `{"country": ["United States", "Canada"], "browser": ["Chrome"]}` — '
    "US/CA clicks from Chrome\n"
    '- `{"short_code": ["link1", "link2"]}` — Stats for specific URLs '
    "(`scope=all` only)\n\n"
    "**Alternative:** You can also pass filters as individual query parameters "
    "(see `browser`, `os`, `country`, `city`, `referrer` parameters below)."
)

STATS_BROWSER_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated browser names. Alternative to using the `filters` JSON "
    "parameter.\n\n"
    "**Important:** Values are case-sensitive. Common values include: "
    "Chrome, Firefox, Safari, Edge, Opera, Samsung Internet.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_OS_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated operating system names. Alternative to using the `filters` "
    "JSON parameter.\n\n"
    "**Important:** Values are case-sensitive. Common values include: "
    "Windows, macOS, Linux, iOS, Android, Chrome OS.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_COUNTRY_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated country names. Alternative to using the `filters` JSON "
    "parameter.\n\n"
    "**Important:** Values are case-sensitive. Use full country names as stored "
    "in the database (e.g., United States, Canada, United Kingdom, India, "
    "Germany, France, Japan).\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_CITY_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated city names. Alternative to using the `filters` JSON "
    "parameter.\n\n"
    "**Important:** Values are case-sensitive. Use exact capitalization as "
    "stored in the database.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_REFERRER_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated referrer URLs. Alternative to using the `filters` JSON "
    "parameter.\n\n"
    "**Important:** Values are case-sensitive. Include the full URL including "
    "protocol.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_DEVICE_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated device types. Alternative to using the `filters` JSON "
    "parameter.\n\n"
    "**Values:** `mobile`, `tablet`, `desktop`, `unknown`. `unknown` also "
    "matches clicks recorded before device tracking existed.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_UTM_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated campaign tag values. Alternative to using the `filters` "
    "JSON parameter.\n\n"
    "**Important:** Values are case-sensitive. `(none)` matches clicks with "
    "no tag.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)

STATS_DOMAIN_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated domains the short links were served on (the system "
    "default domain or your custom domains). Alternative to using the "
    "`filters` JSON parameter.\n\n"
    "**Important:** Values are case-insensitive (normalized to lowercase). "
    "`unknown` matches clicks recorded before domain tracking existed.\n\n"
    "**Note:** Both `filters` JSON and individual parameters can be combined."
)


# ── ListUrlsQuery ────────────────────────────────────────────────────────────

LIST_URLS_FILTER_DESC = (
    "JSON string containing filter criteria for URLs. "
    'Format: `{"field": value}`\n\n'
    "**Available filter fields:**\n\n"
    '- **status** — Filter by URL status (`"ACTIVE"` or `"INACTIVE"`)\n'
    "- **createdAfter** — Filter URLs created after this date "
    "(ISO 8601 datetime or Unix timestamp)\n"
    "- **createdBefore** — Filter URLs created before this date "
    "(ISO 8601 datetime or Unix timestamp)\n"
    "- **passwordSet** — Filter by password protection (boolean: `true`/`false`)\n"
    "- **maxClicksSet** — Filter by click limit presence (boolean: `true`/`false`)\n"
    "- **search** — Search in alias or long_url (case-insensitive string)\n\n"
    "**Value formats:**\n\n"
    '- **status**: String — `"ACTIVE"` or `"INACTIVE"` (case-sensitive)\n'
    "- **createdAfter / createdBefore**: ISO 8601 datetime string "
    '(e.g., `"2024-01-01T00:00:00Z"`) or Unix timestamp (e.g., `1704067200`)\n'
    "- **passwordSet / maxClicksSet**: Boolean — `true` or `false`\n"
    "- **search**: String — case-insensitive search term\n\n"
    "**Examples:**\n\n"
    '- `{"status": "ACTIVE"}` — Only active URLs\n'
    '- `{"passwordSet": true}` — Only password-protected URLs\n'
    '- `{"createdAfter": "2024-01-01T00:00:00Z"}` — URLs created after Jan 1, 2024\n'
    '- `{"status": "ACTIVE", "maxClicksSet": true}` — Active URLs with click limits\n'
    '- `{"search": "example"}` — URLs containing "example" in alias or long_url\n'
    '- `{"createdAfter": "2024-01-01", "createdBefore": "2024-12-31", '
    '"status": "ACTIVE"}` — Active URLs from 2024'
)
