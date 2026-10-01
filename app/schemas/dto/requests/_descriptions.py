"""
Long-form OpenAPI descriptions for query parameter fields.

Extracted here to keep Pydantic model definitions concise while still
producing rich, detailed API documentation via FastAPI's auto-generated
OpenAPI spec.  Each constant is imported into the relevant model's
``Field(description=...)`` argument.
"""

# ── StatsQuery / ExportQuery ─────────────────────────────────────────────────

STATS_SHORT_CODE_DESC = (
    "Comma-separated URL aliases to filter stats to specific URLs you own. "
    "Slices your own aggregate — aliases you do not own simply match "
    "nothing.\n\n"
    "For statistics on a single link, prefer `GET /api/v1/stats/links/{url_id}`."
)

STATS_URL_ID_DESC = (
    "Comma-separated URL ids (MongoDB ObjectIds) to filter stats to specific "
    "URLs you own. Slices your own aggregate — ids you do not own simply "
    "match nothing.\n\n"
    "For statistics on a single link, prefer `GET /api/v1/stats/links/{url_id}`."
)

STATS_TAG_ID_DESC = (
    "Comma-separated tag ids (from `GET /api/v1/tags`). Same scope as `tag`, "
    "by id. Filter only."
)

STATS_TAG_DESC = (
    "Comma-separated tag names. Scopes the aggregate to clicks on your links "
    "carrying at least one of them (resolved to link ids at query time, so a "
    "tag added today covers the link's whole click history). Filter only: "
    "`tag` is not a `group_by` dimension. See also `tag_id`."
)

STATS_START_DATE_DESC = (
    "Start of time range. Accepts ISO 8601 datetime string "
    "(e.g., `2025-01-01T00:00:00Z`) or Unix timestamp in seconds "
    "(e.g., `1735689600`). If omitted, defaults to 7 days before `end_date`."
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
    "- `short_code` — group by URL alias\n"
    "- `utm_source` — group by the `utm_source` tag on the short link "
    "(untagged clicks appear as `(none)`)\n"
    "- `utm_medium` — group by the `utm_medium` tag\n"
    "- `utm_campaign` — group by the `utm_campaign` tag\n"
    "- `variant` — group by the A/B variant served, as its index into "
    "`ab_variants` (`0`, `1`, ...); clicks sent to the default destination "
    "appear as `(default)`. The index is positional: editing `ab_variants` "
    "re-keys past clicks to whatever now sits at that index\n\n"
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
    "- `short_code` — Filter by URL alias (e.g., mylink, promo2024)\n"
    "- `url_id` — Filter by URL id (MongoDB ObjectId); ids you do not own "
    "match nothing\n"
    "- `tag` / `tag_id` — Filter by link tag (name or id); clicks on your links "
    "carrying any listed tag\n"
    "- `utm_source` / `utm_medium` / `utm_campaign` — Filter by campaign tags; "
    "`(none)` matches untagged clicks\n"
    "- `variant` — Filter by A/B variant index (`0`, `1`, ...); "
    "`(default)` matches clicks sent to the default destination\n\n"
    "**Value format:** Array of strings for each dimension.\n\n"
    "**Important:** Filter values are case-sensitive. Use exact capitalization "
    "as stored in the database.\n\n"
    "**Examples:**\n\n"
    '- `{"browser": ["Chrome", "Firefox"]}` — Chrome OR Firefox clicks\n'
    '- `{"country": ["United States", "Canada"], "browser": ["Chrome"]}` — '
    "US/CA clicks from Chrome\n"
    '- `{"short_code": ["link1", "link2"]}` — Stats for specific URLs\n\n'
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

STATS_VARIANT_DESC = (
    "**Method 2: Individual Filter Parameter**\n\n"
    "Comma-separated A/B variant indices (`0`, `1`, ...). `(default)` matches "
    "clicks sent to the default destination, including every click on a link "
    "without variants.\n\n"
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


# ── LinkStatsQuery / LinkExportQuery ─────────────────────────────────────────
# The per-link endpoints select the link in the path, so the link-identity
# dimensions (`short_code`, `url_id`) disappear from group_by and filters.

LINK_STATS_GROUP_BY_DESC = (
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
    "- `utm_source` — group by the `utm_source` tag on the short link "
    "(untagged clicks appear as `(none)`)\n"
    "- `utm_medium` — group by the `utm_medium` tag\n"
    "- `utm_campaign` — group by the `utm_campaign` tag\n"
    "- `variant` — group by the A/B variant served, as its index into "
    "`ab_variants` (`0`, `1`, ...); clicks sent to the default destination "
    "appear as `(default)`. The index is positional: editing `ab_variants` "
    "re-keys past clicks to whatever now sits at that index\n\n"
    "Multiple dimensions can be combined: `time,browser` returns time series "
    "broken down by browser."
)

LINK_STATS_FILTERS_DESC = (
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
    "- `utm_source` / `utm_medium` / `utm_campaign` — Filter by campaign tags; "
    "`(none)` matches untagged clicks\n"
    "- `variant` — Filter by A/B variant index (`0`, `1`, ...); "
    "`(default)` matches clicks sent to the default destination\n\n"
    "**Value format:** Array of strings for each dimension.\n\n"
    "**Important:** Filter values are case-sensitive. Use exact capitalization "
    "as stored in the database.\n\n"
    "**Examples:**\n\n"
    '- `{"browser": ["Chrome", "Firefox"]}` — Chrome OR Firefox clicks\n'
    '- `{"country": ["United States", "Canada"], "browser": ["Chrome"]}` — '
    "US/CA clicks from Chrome\n\n"
    "**Alternative:** You can also pass filters as individual query parameters "
    "(see `browser`, `os`, `country`, `city`, `referrer` parameters below)."
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
    "- **search** — Search in alias or long_url (case-insensitive string)\n"
    "- **tagIds** — Only links carrying these tags, by id (array of strings)\n"
    "- **tagNames** — Same, by tag name; unknown names match nothing\n"
    '- **tagsMatch** — `"any"` (default) or `"all"`; how multiple tags combine\n\n'
    "**Value formats:**\n\n"
    '- **status**: String — `"ACTIVE"` or `"INACTIVE"` (case-sensitive)\n'
    "- **createdAfter / createdBefore**: ISO 8601 datetime string "
    '(e.g., `"2024-01-01T00:00:00Z"`) or Unix timestamp (e.g., `1704067200`)\n'
    "- **passwordSet / maxClicksSet**: Boolean — `true` or `false`\n"
    "- **search**: String — case-insensitive search term\n"
    "- **tagIds / tagNames**: Array of strings\n"
    '- **tagsMatch**: String — `"any"` or `"all"`\n\n'
    "**Examples:**\n\n"
    '- `{"status": "ACTIVE"}` — Only active URLs\n'
    '- `{"passwordSet": true}` — Only password-protected URLs\n'
    '- `{"createdAfter": "2024-01-01T00:00:00Z"}` — URLs created after Jan 1, 2024\n'
    '- `{"status": "ACTIVE", "maxClicksSet": true}` — Active URLs with click limits\n'
    '- `{"search": "example"}` — URLs containing "example" in alias or long_url\n'
    '- `{"tagNames": ["launch", "q3"], "tagsMatch": "all"}` — URLs tagged both launch and q3\n'
    '- `{"createdAfter": "2024-01-01", "createdBefore": "2024-12-31", '
    '"status": "ACTIVE"}` — Active URLs from 2024'
)
