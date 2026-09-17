"""El processor `redact_secrets` enmascara claves sensibles y valores con
pinta de bearer/JWT antes de que lleguen al renderer JSON
(threat-model.md C-14)."""

from __future__ import annotations

import pytest

from safent_ads.logging_setup import _REDACTED, redact_secrets


class _FakeLogger:
    pass


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "Token",
        "api_key",
        "secret",
        "password",
        "Authorization",
        "mcp_token",
        # Spec 002 (mcp_oauth) threat-model.md C-50: code/verifier/txn/assertion.
        "code",
        "code_verifier",
        "code_challenge",
        "txn",
        "txn_id",
        "pkce_verifier",
        "assertion",
    ],
)
def test_redacts_values_of_sensitive_keys(key: str) -> None:
    event_dict = redact_secrets(_FakeLogger(), "info", {key: "super-secret-value"})

    assert event_dict[key] == _REDACTED


@pytest.mark.parametrize(
    "key", ["error_code", "status_code", "rule_code", "platform_code", "zip_code", "pairing_code"]
)
def test_does_not_redact_business_codes_that_only_contain_the_word_code(key: str) -> None:
    """`_SENSITIVE_KEY_PATTERN` amplio con `code`/`txn` (C-50) no puede
    redactar estas claves de negocio reales -- ninguna de ellas es un
    codigo de autorizacion OAuth, y ya se registran sin cifrar en decenas
    de sitios de este repo (`accounts/presentation/connections_router.py`,
    `orchestration/infrastructure/rule_step.py`...)."""
    event_dict = redact_secrets(_FakeLogger(), "info", {key: "NOT_A_SECRET"})

    assert event_dict[key] == "NOT_A_SECRET"


def test_redacts_bearer_token_by_value_even_under_a_safe_key() -> None:
    event_dict = redact_secrets(
        _FakeLogger(), "info", {"header": "Bearer abc123.def456-ghi_789"}
    )

    assert event_dict["header"] == _REDACTED


def test_redacts_jwt_like_string_by_value() -> None:
    jwt_like = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    event_dict = redact_secrets(_FakeLogger(), "info", {"raw_header": jwt_like})

    assert event_dict["raw_header"] == _REDACTED


def test_redacts_nested_values_inside_dicts_and_lists() -> None:
    event_dict = redact_secrets(
        _FakeLogger(),
        "info",
        {"context": {"nested": ["fine", "Bearer abc.def-ghi_jkl"], "password": "x"}},
    )

    assert event_dict["context"]["nested"][0] == "fine"
    assert event_dict["context"]["nested"][1] == _REDACTED
    assert event_dict["context"]["password"] == _REDACTED


def test_leaves_ordinary_fields_untouched() -> None:
    event_dict = redact_secrets(
        _FakeLogger(), "info", {"business_id": "abc-123", "event": "signal_emitted"}
    )

    assert event_dict == {"business_id": "abc-123", "event": "signal_emitted"}
