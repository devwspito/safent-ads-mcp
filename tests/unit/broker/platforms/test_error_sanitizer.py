"""`redact_sdk_error` nunca deja pasar tokens, `refresh_token` o ids de
cuenta (threat-model.md C-13)."""

from __future__ import annotations

from safent_ads.broker.platforms.error_sanitizer import redact_sdk_error


def test_sdk_error_redacted() -> None:
    exc = RuntimeError(
        "GoogleAdsException: client_secret: 'AbCdEfGhIjKlMnOpQrSt', "
        "refresh_token=1//0abcXYZ-fake-refresh, customer_id 123-456-7890"
    )

    redacted = redact_sdk_error(exc)

    assert "AbCdEfGhIjKlMnOpQrSt" not in redacted
    assert "1//0abcXYZ-fake-refresh" not in redacted
    assert "123-456-7890" not in redacted
    assert "RuntimeError" in redacted


def test_redacts_bearer_header() -> None:
    exc = ValueError("HTTP 401: Authorization: Bearer abc123.def456-ghi_789")

    redacted = redact_sdk_error(exc)

    assert "abc123.def456-ghi_789" not in redacted


def test_redacts_meta_access_token() -> None:
    exc = RuntimeError("FacebookRequestError: token EAABsomeLongMetaSystemUserTokenValue123")

    redacted = redact_sdk_error(exc)

    assert "EAABsomeLongMetaSystemUserTokenValue123" not in redacted


def test_truncates_long_messages() -> None:
    exc = RuntimeError("x" * 5000)

    redacted = redact_sdk_error(exc)

    assert len(redacted) <= 300
