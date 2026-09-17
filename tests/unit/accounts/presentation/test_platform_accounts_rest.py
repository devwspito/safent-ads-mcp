"""`platform_account_to_json` (contracts/rest-api.md §Conexiones lineas
330-342): mapeo puro `PlatformAccountConnectionView` -> forma REST exacta
de `panel/src/api/schemas/connections.ts::platformAccountSchema`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.accounts.application.list_platform_accounts import PlatformAccountConnectionView
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccountStatus
from safent_ads.accounts.domain.platform_credential import CredentialStatus
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.presentation.platform_accounts_rest import platform_account_to_json
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _view(**overrides: object) -> PlatformAccountConnectionView:
    defaults: dict[str, object] = {
        "account_ref": AccountRef(PlatformCode.GOOGLE, "123"),
        "label": "google:123",
        "status": PlatformAccountStatus.ACTIVE,
        "currency": "EUR",
        "timezone": "Europe/Madrid",
        "api_tier": ApiTier.GOOGLE_STANDARD,
        "credential_status": CredentialStatus.CONNECTED,
        "credential_expires_at": None,
        "credential_last_validated_at": None,
        "credential_checked_at": None,
        "credential_last_error_code": None,
        "needs_reconnect": False,
        "last_synced_at": None,
    }
    defaults.update(overrides)
    return PlatformAccountConnectionView(**defaults)  # type: ignore[arg-type]


def test_maps_identity_and_scalar_fields() -> None:
    body = platform_account_to_json(_view(), now=_NOW)

    assert body["platform_account_id"] == "google:123"
    assert body["platform"] == "google"
    assert body["external_account_id"] == "123"
    assert body["status"] == "ACTIVE"
    assert body["api_tier"] == "google_standard"
    assert body["currency"] == "EUR"
    assert body["timezone"] == "Europe/Madrid"


def test_connected_without_expiry_is_ok() -> None:
    body = platform_account_to_json(_view(), now=_NOW)

    assert body["token"]["health"] == "ok"
    assert body["token"]["expires_at"] is None


def test_connected_expiring_within_a_week_is_expiring_soon() -> None:
    view = _view(credential_expires_at=_NOW + timedelta(days=3))

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "expiring_soon"


def test_connected_with_a_past_expiry_is_expired() -> None:
    view = _view(credential_expires_at=_NOW - timedelta(hours=1))

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "expired"


def test_connected_far_from_expiry_is_ok() -> None:
    view = _view(credential_expires_at=_NOW + timedelta(days=30))

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "ok"


def test_expired_credential_status_maps_to_expired() -> None:
    view = _view(credential_status=CredentialStatus.EXPIRED)

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "expired"


def test_revoked_credential_status_maps_to_revoked() -> None:
    view = _view(credential_status=CredentialStatus.REVOKED)

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "revoked"


def test_invalid_credential_status_maps_to_expired() -> None:
    """`CredentialStatus.INVALID` no tiene hueco propio en `tokenHealthSchema`
    (zod): "expired" es el mas honesto de los 4 disponibles (exige
    reconectar, discrepancia documentada en el informe de esta rama)."""
    view = _view(credential_status=CredentialStatus.INVALID)

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "expired"


def test_missing_credential_fails_closed_to_revoked() -> None:
    view = _view(credential_status=None)

    body = platform_account_to_json(view, now=_NOW)

    assert body["token"]["health"] == "revoked"


def test_checked_at_falls_back_to_now_when_never_validated() -> None:
    body = platform_account_to_json(_view(credential_last_validated_at=None), now=_NOW)

    assert body["token"]["checked_at"] == _NOW.isoformat()


def test_checked_at_uses_last_validated_at_when_present() -> None:
    validated_at = _NOW - timedelta(days=1)
    body = platform_account_to_json(_view(credential_last_validated_at=validated_at), now=_NOW)

    assert body["token"]["checked_at"] == validated_at.isoformat()


def test_last_synced_at_null_when_never_synced() -> None:
    body = platform_account_to_json(_view(last_synced_at=None), now=_NOW)

    assert body["last_synced_at"] is None


def test_checked_at_prefers_the_real_column_over_last_validated_at() -> None:
    checked_at = _NOW - timedelta(hours=2)
    body = platform_account_to_json(
        _view(
            credential_checked_at=checked_at,
            credential_last_validated_at=_NOW - timedelta(days=1),
        ),
        now=_NOW,
    )

    assert body["token"]["checked_at"] == checked_at.isoformat()


def test_last_error_code_reflects_the_persisted_value_from_the_cron() -> None:
    body = platform_account_to_json(
        _view(
            credential_status=CredentialStatus.EXPIRED,
            credential_last_error_code="TOKEN_EXPIRED",
        ),
        now=_NOW,
    )

    assert body["last_error_code"] == "TOKEN_EXPIRED"


def test_quota_and_unavailable_levers_are_honest_placeholders() -> None:
    """Sin fuente persistida todavia (contrato, discrepancia documentada):
    `None`/`[]`, nunca un numero inventado."""
    body = platform_account_to_json(_view(), now=_NOW)

    assert body["quota"] == {"window": "daily", "used_pct": None, "writes_remaining": None}
    assert body["unavailable_levers"] == []
    assert body["last_error_code"] is None
