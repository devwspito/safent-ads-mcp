"""`_webhook_token_rate_limit_key` (composition/api.py, T220): `POST
/conversions/webhook` no lleva sesion, asi que se limita por el propio
`X-Webhook-Token` presentado (o IP si falta), nunca por bearer/sesion
estandar (`_rate_limit_key` solo mira `authorization`)."""

from __future__ import annotations

from safent_ads.composition.api import (
    _CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY,
    TokenBucketRateLimiter,
    _webhook_token_rate_limit_key,
)


def _scope(headers: dict[bytes, bytes]) -> dict[str, object]:
    return {
        "type": "http",
        "headers": list(headers.items()),
        "client": ("203.0.113.5", 1234),
    }


def test_key_uses_webhook_token_header_when_present() -> None:
    key = _webhook_token_rate_limit_key(_scope({b"x-webhook-token": b"secret-token"}))

    assert key == "secret-token"


def test_key_falls_back_to_ip_without_a_token() -> None:
    key = _webhook_token_rate_limit_key(_scope({}))

    assert key == "203.0.113.5"


def test_budget_is_exhausted_after_capacity_calls() -> None:
    limiter = TokenBucketRateLimiter(
        capacity=_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY,
        refill_per_second=_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY / 60,
    )
    key = "some-token"

    allowed = [limiter.allow(key) for _ in range(_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY)]

    assert all(allowed)
    assert limiter.allow(key) is False


def test_a_flooded_token_does_not_exhaust_another_tokens_budget() -> None:
    limiter = TokenBucketRateLimiter(
        capacity=_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY,
        refill_per_second=_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY / 60,
    )

    for _ in range(_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY):
        assert limiter.allow("flooded-token") is True
    exhausted = limiter.allow("flooded-token")
    still_allowed = limiter.allow("another-token")

    assert exhausted is False
    assert still_allowed is True
