"""Explicit Search criteria. Omission preserves old plans; nothing is inferred."""

import re
from typing import Any

_MAX_KEYWORDS = 50
_MAX_LOCATIONS = 25
_MAX_KEYWORD_LENGTH = 80
_CONTROL_CHARACTER_BOUNDARY = 32


def valid_keywords(value: object) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_KEYWORDS:
        return False
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"text", "match_type"}:
            return False
        text, match = item["text"], item["match_type"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or text != text.strip()
            or len(text) > _MAX_KEYWORD_LENGTH
            or any(ord(char) < _CONTROL_CHARACTER_BOUNDARY for char in text)
            or not isinstance(match, str)
            or match not in {"EXACT", "PHRASE", "BROAD"}
        ):
            return False
        key = text.casefold(), match
        if key in seen:
            return False
        seen.add(key)
    return True


def valid_geography(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "geo_target_constants",
        "positive_geo_target_type",
    }:
        return False
    locations: Any = value["geo_target_constants"]
    return (
        value["positive_geo_target_type"] == "PRESENCE"
        and isinstance(locations, list)
        and 1 <= len(locations) <= _MAX_LOCATIONS
        and all(
            isinstance(item, str) and re.fullmatch(r"geoTargetConstants/[1-9][0-9]{0,19}", item)
            for item in locations
        )
        and len(set(locations)) == len(locations)
    )
