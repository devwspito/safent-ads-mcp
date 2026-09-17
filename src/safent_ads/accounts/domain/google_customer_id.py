"""Normalize an explicit Google Ads customer selection without inferring one."""

import re

from safent_ads.shared.ids import PlatformCode

_CUSTOMER_ID = re.compile(r"(?:[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4})\Z")


def normalize_google_customer_id(value: str | None, *, provider: PlatformCode) -> str | None:
    if value is None:
        return None
    if provider != PlatformCode.GOOGLE:
        raise ValueError("google_customer_id_only_google")
    if not isinstance(value, str) or _CUSTOMER_ID.fullmatch(value.strip()) is None:
        raise ValueError("google_customer_id_invalid")
    return value.strip().replace("-", "")
