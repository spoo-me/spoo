"""
Legacy helper functions preserved for backward-compatible routes.

These functions were originally in utils/general.py, utils/analytics_utils.py,
and utils/pipeline_utils.py. They are used only by routes/legacy/ endpoints.
"""

from datetime import datetime, timedelta
from typing import Any


def is_positive_integer(value) -> bool:
    try:
        int(value)
        return not int(value) < 0
    except ValueError:
        return False
    except TypeError:
        return False


def humanize_number(num) -> str:
    magnitude = 0
    while abs(num) >= 1000:
        magnitude += 1
        num /= 1000.0
    return f"{int(num)}{['', 'K', 'M', 'B', 'T', 'P'][magnitude]}+"


def convert_country_data(data) -> list[dict[str, Any]]:
    from shared.aggregation_strategies import convert_country_name

    return [{"id": convert_country_name(k), "value": v} for k, v in data.items()]


def add_missing_dates(key, url_data):
    counter = url_data[key]
    first_click_date = url_data["creation-date"]
    last_click_date = datetime.now().strftime("%Y-%m-%d")

    first_date = datetime.strptime(first_click_date, "%Y-%m-%d")
    last_date = datetime.strptime(last_click_date, "%Y-%m-%d")

    date_range = [
        first_date + timedelta(days=x) for x in range((last_date - first_date).days + 1)
    ]
    all_dates = [date.strftime("%Y-%m-%d") for date in date_range]

    for date in all_dates:
        if date not in counter:
            counter[date] = 0

    sorted_counter = {date: counter[date] for date in sorted(counter.keys())}
    url_data[key] = sorted_counter
    return url_data


def top_four(dictionary):
    if len(dictionary) < 6:
        return dictionary
    sorted_dict = dict(sorted(dictionary.items(), key=lambda x: x[1], reverse=True))
    new_dict = {}
    others = 0
    for i, (key, value) in enumerate(sorted_dict.items()):
        if i < 4:
            new_dict[key] = value
        else:
            others += value

    new_dict["others"] = others
    return new_dict


def calculate_click_averages(data):
    total_clicks = data["total-clicks"]
    creation_date = datetime.fromisoformat(data["creation-date"]).date()
    current_date = datetime.now().date()
    link_age = (current_date - creation_date).days + 1

    avg_weekly_clicks = round(total_clicks / 7, 2)
    avg_daily_clicks = round(total_clicks / link_age, 2)
    avg_monthly_clicks = round(total_clicks / 30, 2)

    return avg_daily_clicks, avg_weekly_clicks, avg_monthly_clicks


def _create_field_transform(field_name):
    return {
        f"{field_name}": {
            "$arrayToObject": {
                "$map": {
                    "input": {"$objectToArray": f"${field_name}"},
                    "as": "item",
                    "in": {"k": "$$item.k", "v": "$$item.v.counts"},
                }
            }
        },
        f"unique_{field_name}": {
            "$arrayToObject": {
                "$map": {
                    "input": {"$objectToArray": f"${field_name}"},
                    "as": "item",
                    "in": {
                        "k": "$$item.k",
                        "v": {"$size": {"$setUnion": ["$$item.v.ips"]}},
                    },
                }
            }
        },
    }


def get_stats_pipeline(short_code):
    fields = ["browser", "os_name", "country", "referrer"]
    add_fields = {}
    for field in fields:
        add_fields |= _create_field_transform(field)

    return [
        {"$match": {"_id": short_code}},
        {
            "$project": {
                "url": 1,
                # Without this every consumer of the pipeline is structurally
                # unable to honour a takedown.
                "blocked": {"$ifNull": ["$blocked", False]},
                "browser": {"$ifNull": ["$browser", {}]},
                "os_name": {"$ifNull": ["$os_name", {}]},
                "country": {"$ifNull": ["$country", {}]},
                "referrer": {"$ifNull": ["$referrer", {}]},
                "total_unique_clicks": {"$size": "$ips"},
                "total-clicks": {"$ifNull": ["$total-clicks", 0]},
                "max-clicks": {"$ifNull": ["$max-clicks", None]},
                "expiration-time": {"$ifNull": ["$expiration-time", None]},
                "password": {"$ifNull": ["$password", None]},
                "short_code": {"$ifNull": ["$short_code", None]},
                "last-click-browser": {"$ifNull": ["$last-click-browser", None]},
                "last-click-os": {"$ifNull": ["$last-click-os", None]},
                "last-click-country": {"$ifNull": ["$last-click-country", None]},
                "block-bots": {"$ifNull": ["$block-bots", False]},
                "bots": {"$ifNull": ["$bots", {}]},
                "counter": {"$ifNull": ["$counter", {}]},
                "unique_counter": {"$ifNull": ["$unique_counter", {}]},
                "average_redirection_time": {
                    "$ifNull": ["$average_redirection_time", 0]
                },
                "creation-date": {"$ifNull": ["$creation-date", None]},
                "creation-time": {"$ifNull": ["$creation-time", None]},
                "last-click": {"$ifNull": ["$last-click", None]},
            }
        },
        {"$addFields": add_fields},
    ]
