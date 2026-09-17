"""`validate_id_token_claims` (002b tasks.md T017/T018, research.md Decisión D):
funcion PURA sobre el diccionario de claims del `id_token`. Ni red, ni httpx,
ni reloj del sistema -- el transporte vive en
`iam/infrastructure/google_oidc_provider.py` y no se toca aqui."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from safent_ads.iam.application.federated_id_token import (
    FederatedIdTokenInvalidError,
    validate_id_token_claims,
)
from safent_ads.iam.application.ports import FederatedIdentityClaims
from safent_ads.iam.domain.federated_identity import FederatedIssuer
from safent_ads.iam.domain.federated_transaction import ReferenceHash

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_AUDIENCE = "client-de-identidad.apps.googleusercontent.com"
_NONCE = "nonce-en-claro-de-la-transaccion"
_NONCE_HASH = ReferenceHash.of(_NONCE)


class _Absent:
    """Centinela para pedir que una claim NO aparezca en el diccionario."""


_ABSENT = _Absent()


def _claims(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401 - claims heterogeneos
    base: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": _AUDIENCE,
        "exp": int((_NOW + timedelta(minutes=5)).timestamp()),
        "nonce": _NONCE,
        "sub": "112233445566778899000",
        "email": "Duenyo@Example.com",
        "email_verified": True,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not _ABSENT}


def _validate(claims: dict[str, Any]) -> FederatedIdentityClaims:
    return validate_id_token_claims(
        claims,
        expected_audience=_AUDIENCE,
        expected_nonce_hash=_NONCE_HASH,
        now=_NOW,
    )


def test_accepts_the_issuer_with_scheme() -> None:
    identity = _validate(_claims(iss="https://accounts.google.com"))

    assert identity.issuer is FederatedIssuer.GOOGLE
    assert str(identity.subject) == "112233445566778899000"
    assert str(identity.email) == "duenyo@example.com"
    assert identity.email_verified is True


def test_accepts_the_issuer_without_scheme() -> None:
    identity = _validate(_claims(iss="accounts.google.com"))

    assert identity.issuer is FederatedIssuer.GOOGLE


def test_rejects_an_unknown_issuer() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(iss="https://accounts.google.com.evil.test"))


def test_rejects_a_missing_issuer() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(iss=_ABSENT))


def test_rejects_an_audience_that_is_not_our_client() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(aud="otro-cliente.apps.googleusercontent.com"))


def test_rejects_an_audience_that_is_not_a_string() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(aud=[_AUDIENCE]))


def test_azp_mismatch_is_rejected() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(azp="otro-cliente.apps.googleusercontent.com"))


def test_azp_absent_is_accepted() -> None:
    identity = _validate(_claims(azp=_ABSENT))

    assert identity.subject is not None


def test_azp_matching_the_expected_audience_is_accepted() -> None:
    identity = _validate(_claims(azp=_AUDIENCE))

    assert identity.subject is not None


def test_accepts_an_expiry_in_the_future() -> None:
    identity = _validate(_claims(exp=int((_NOW + timedelta(seconds=1)).timestamp())))

    assert identity.email_verified is True


def test_rejects_an_expiry_in_the_past() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(exp=int((_NOW - timedelta(seconds=1)).timestamp())))


def test_rejects_an_expiry_exactly_now() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(exp=int(_NOW.timestamp())))


def test_rejects_a_missing_expiry() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(exp=_ABSENT))


def test_rejects_a_boolean_expiry() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(exp=True))


def test_rejects_a_nonce_that_does_not_match() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(nonce="nonce-de-otra-transaccion"))


def test_rejects_a_missing_nonce() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(nonce=_ABSENT))


def test_email_verified_true_is_verified() -> None:
    assert _validate(_claims(email_verified=True)).email_verified is True


def test_email_verified_as_the_string_true_is_verified() -> None:
    assert _validate(_claims(email_verified="true")).email_verified is True


def test_email_verified_false_is_not_verified() -> None:
    assert _validate(_claims(email_verified=False)).email_verified is False


def test_email_verified_absent_is_not_verified() -> None:
    assert _validate(_claims(email_verified=_ABSENT)).email_verified is False


def test_rejects_a_missing_subject() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(sub=_ABSENT))


def test_rejects_an_empty_subject() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(sub="   "))


def test_rejects_a_missing_email() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(email=_ABSENT))


def test_rejects_a_malformed_email() -> None:
    with pytest.raises(FederatedIdTokenInvalidError):
        _validate(_claims(email="no-es-un-correo"))


def test_the_error_never_carries_the_nonce_in_the_clear() -> None:
    with pytest.raises(FederatedIdTokenInvalidError) as failure:
        _validate(_claims(nonce="nonce-de-otra-transaccion"))

    assert _NONCE not in str(failure.value)
    assert "nonce-de-otra-transaccion" not in str(failure.value)
