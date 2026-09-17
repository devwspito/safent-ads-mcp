"""`AuthorizationRequest` (data-model.md, tasks.md T004): PENDING ->
CONSENTED -> REDEEMED; un codigo se canjea una sola vez
(threat-model.md C-38); caducidad de la solicitud y del codigo."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.mcp_oauth.domain.authorization import (
    AuthorizationRequest,
    AuthorizationRequestState,
)
from safent_ads.mcp_oauth.domain.errors import (
    AuthorizationRequestExpiredError,
    AuthorizationRequestNotConsentedError,
    AuthorizationRequestNotPendingError,
    CodeAlreadyRedeemedError,
    InvalidCodeChallengeError,
)
from safent_ads.mcp_oauth.domain.grant import TokenHash
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet

_CREATED_AT = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_REQUEST_TTL = timedelta(minutes=10)
_CODE_TTL = timedelta(seconds=60)
_CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_RESOURCE = ResourceIndicator("https://ads.example.com/mcp")


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _request(**overrides: object) -> AuthorizationRequest:
    defaults: dict[str, object] = {
        "txn_id": uuid.uuid4(),
        "client_id": "client-1",
        "redirect_uri": "http://127.0.0.1:43123/callback",
        "code_challenge": _s256_challenge(_CODE_VERIFIER),
        "client_state": "xyz",
        "scope_set": ScopeSet.parse("ads:read"),
        "resource": _RESOURCE,
        "created_at": _CREATED_AT,
        "expires_at": _CREATED_AT + _REQUEST_TTL,
    }
    defaults.update(overrides)
    return AuthorizationRequest(**defaults)  # type: ignore[arg-type]


def _consent(request: AuthorizationRequest, *, now: datetime) -> None:
    request.consent(
        owner_id=uuid.uuid4(), code_hash=TokenHash("a" * 64), now=now, code_ttl=_CODE_TTL
    )


def test_starts_pending() -> None:
    request = _request()

    assert request.state is AuthorizationRequestState.PENDING


def test_consent_moves_to_consented_and_fixes_owner_and_code() -> None:
    request = _request()
    owner_id = uuid.uuid4()

    request.consent(
        owner_id=owner_id, code_hash=TokenHash("b" * 64), now=_CREATED_AT, code_ttl=_CODE_TTL
    )

    assert request.state is AuthorizationRequestState.CONSENTED
    assert request.owner_id == owner_id
    assert request.code_hash == TokenHash("b" * 64)


def test_redeem_moves_to_redeemed() -> None:
    request = _request()
    _consent(request, now=_CREATED_AT)

    request.redeem(_CREATED_AT + timedelta(seconds=1))

    assert request.state is AuthorizationRequestState.REDEEMED


def test_redeeming_twice_raises_code_already_redeemed() -> None:
    request = _request()
    _consent(request, now=_CREATED_AT)
    request.redeem(_CREATED_AT + timedelta(seconds=1))

    with pytest.raises(CodeAlreadyRedeemedError):
        request.redeem(_CREATED_AT + timedelta(seconds=2))


def test_redeeming_without_consent_raises() -> None:
    request = _request()

    with pytest.raises(AuthorizationRequestNotConsentedError):
        request.redeem(_CREATED_AT)


def test_consenting_twice_raises_not_pending() -> None:
    request = _request()
    _consent(request, now=_CREATED_AT)

    with pytest.raises(AuthorizationRequestNotPendingError):
        _consent(request, now=_CREATED_AT)


def test_deny_moves_to_denied() -> None:
    request = _request()

    request.deny(_CREATED_AT)

    assert request.state is AuthorizationRequestState.DENIED


def test_pending_request_expires_after_ttl() -> None:
    """Nit de la revision de seguridad (16-sep): `_reject_if_expired` es
    PURA -- levanta la excepcion pero ya no muta `state` como efecto
    secundario (nadie capturaba esa mutacion para persistirla; el estado
    EXPIRED real lo pone `prune_stale_clients.py`, barrido SQL directo)."""
    request = _request()
    past_deadline = _CREATED_AT + _REQUEST_TTL + timedelta(seconds=1)

    with pytest.raises(AuthorizationRequestExpiredError):
        request.deny(past_deadline)

    assert request.state is AuthorizationRequestState.PENDING


def test_consented_code_expires_after_ttl() -> None:
    request = _request()
    _consent(request, now=_CREATED_AT)
    past_code_deadline = _CREATED_AT + _CODE_TTL + timedelta(seconds=1)

    with pytest.raises(AuthorizationRequestExpiredError):
        request.redeem(past_code_deadline)

    assert request.state is AuthorizationRequestState.CONSENTED


def test_verify_pkce_accepts_matching_verifier() -> None:
    request = _request()

    assert request.verify_pkce(_CODE_VERIFIER) is True


def test_verify_pkce_rejects_wrong_verifier() -> None:
    request = _request()

    assert request.verify_pkce("wrong-verifier-wrong-verifier-wrong-verif") is False


def test_rejects_a_code_challenge_shorter_than_43_base64url_chars() -> None:
    with pytest.raises(InvalidCodeChallengeError):
        _request(code_challenge="too-short")


def test_rejects_a_code_challenge_longer_than_43_base64url_chars() -> None:
    """I5 de la revision de seguridad (16-sep): `code_challenge` es
    EXACTAMENTE 43 caracteres (el digest S256 de 32 bytes en base64url sin
    relleno) -- 43-128 es el rango de `code_verifier`, no el del
    challenge. El CHECK de la base de datos ya lo exigia exacto
    (0035_mcp_oauth); el dominio aceptaba hasta 128 por error."""
    with pytest.raises(InvalidCodeChallengeError):
        _request(code_challenge=_s256_challenge(_CODE_VERIFIER) + "x" * 85)


def test_the_rejected_code_challenge_never_enters_the_error_message() -> None:
    """Revision de seguridad (PR 44, MINOR c): `SdkOAuthProvider._start()`
    reenvia este mensaje tal cual en la redireccion 302 de vuelta al
    cliente (`AuthorizeError.error_description`), que puede acabar en un
    log de borde que capture el `Location` -- mismo criterio que
    `RedirectUri._reject_control_characters`, el valor NO entra en el
    mensaje."""
    poisoned_value = "esto-no-deberia-aparecer-en-ningun-sitio"

    with pytest.raises(InvalidCodeChallengeError) as exc_info:
        _request(code_challenge=poisoned_value)

    assert poisoned_value not in str(exc_info.value)
