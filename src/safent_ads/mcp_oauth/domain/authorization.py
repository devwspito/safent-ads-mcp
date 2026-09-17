"""`AuthorizationRequest` (data-model.md): intento en curso de un cliente
de obtener acceso delegado. Nace PENDING; el consentimiento lo convierte
en CONSENTED con un codigo de un solo uso; canjear, denegar o caducar lo
cierran. `owner_id`/`code_hash` solo se fijan al consentir (invariante de
`oauth_authorization_requests`: `consented_at IS NULL <=> owner_id IS
NULL`)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timedelta
from enum import StrEnum

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

# I5 de la revision de seguridad (16-sep): exactamente 43 caracteres, no
# un rango 43-128. RFC 7636 SS4.1-4.2 fija ese rango para `code_verifier`
# (43-128 caracteres ARBITRARIOS que el cliente elige); `code_challenge`
# S256 es SIEMPRE el SHA-256 del verifier codificado en base64url SIN
# relleno -- un digest de 32 bytes produce, sin excepcion, 43 caracteres.
# El CHECK de la base de datos (0035_mcp_oauth,
# `oauth_authorization_requests_code_challenge_check`) ya exigia
# `{{43}}` exacto; el dominio aceptaba hasta 128 por error.
_CODE_CHALLENGE_PATTERN = re.compile(r"^[A-Za-z0-9\-_]{43}$")


class AuthorizationRequestState(StrEnum):
    PENDING = "PENDING"
    CONSENTED = "CONSENTED"
    REDEEMED = "REDEEMED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class AuthorizationRequest:
    def __init__(
        self,
        *,
        txn_id: uuid.UUID,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        client_state: str | None,
        scope_set: ScopeSet,
        resource: ResourceIndicator,
        created_at: datetime,
        expires_at: datetime,
        state: AuthorizationRequestState = AuthorizationRequestState.PENDING,
        code_hash: TokenHash | None = None,
        owner_id: uuid.UUID | None = None,
        consented_at: datetime | None = None,
        redeemed_at: datetime | None = None,
        code_expires_at: datetime | None = None,
    ) -> None:
        self._validate_code_challenge(code_challenge)
        self.id = txn_id
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.code_challenge = code_challenge
        self.client_state = client_state
        self.scope_set = scope_set
        self.resource = resource
        self.created_at = created_at
        self.expires_at = expires_at
        self.state = state
        self.code_hash = code_hash
        self.owner_id = owner_id
        self.consented_at = consented_at
        self.redeemed_at = redeemed_at
        self.code_expires_at = code_expires_at

    @staticmethod
    def _validate_code_challenge(value: str) -> None:
        # El valor NO entra en el mensaje (mismo motivo que
        # `mcp_oauth/domain/client.py::RedirectUri._reject_control_
        # characters`): `SdkOAuthProvider._start()` reenvia este mensaje
        # tal cual como `AuthorizeError.error_description`, que el SDK
        # (`sdk:handlers/authorize.py::error_response()`) puede reflejar en
        # la redireccion 302 de vuelta al cliente -- de ahi puede acabar en
        # un log de borde que capture el `Location` (revision de
        # seguridad, PR 44 MINOR c).
        if not _CODE_CHALLENGE_PATTERN.match(value):
            raise InvalidCodeChallengeError(
                "code_challenge debe ser base64url de exactamente 43 caracteres "
                "(SHA-256 S256, RFC 7636)"
            )

    def is_expired(self, now: datetime) -> bool:
        if self.state is AuthorizationRequestState.PENDING:
            return now > self.expires_at
        if self.state is AuthorizationRequestState.CONSENTED:
            return self.code_expires_at is not None and now > self.code_expires_at
        return False

    def consent(
        self, *, owner_id: uuid.UUID, code_hash: TokenHash, now: datetime, code_ttl: timedelta
    ) -> None:
        self._require_state(AuthorizationRequestState.PENDING)
        self._reject_if_expired(now)
        self.owner_id = owner_id
        self.code_hash = code_hash
        self.consented_at = now
        self.code_expires_at = now + code_ttl
        self.state = AuthorizationRequestState.CONSENTED

    def deny(self, now: datetime) -> None:
        self._require_state(AuthorizationRequestState.PENDING)
        self._reject_if_expired(now)
        self.state = AuthorizationRequestState.DENIED

    def redeem(self, now: datetime) -> None:
        if self.state is AuthorizationRequestState.REDEEMED:
            raise CodeAlreadyRedeemedError(f"el codigo de {self.id} ya fue canjeado")
        if self.state is not AuthorizationRequestState.CONSENTED:
            raise AuthorizationRequestNotConsentedError(f"solicitud {self.id} sin consentir")
        self._reject_if_expired(now)
        self.state = AuthorizationRequestState.REDEEMED
        self.redeemed_at = now

    def verify_pkce(self, code_verifier: str) -> bool:
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return hmac.compare_digest(computed, self.code_challenge)

    def _require_state(self, expected: AuthorizationRequestState) -> None:
        if self.state is not expected:
            raise AuthorizationRequestNotPendingError(
                f"solicitud {self.id} en estado {self.state}, se esperaba {expected}"
            )

    def _reject_if_expired(self, now: datetime) -> None:
        """PURA: solo comprueba y levanta, nunca muta `self.state` -- la
        fila real que llega a EXPIRED la pone
        `infrastructure/prune_stale_clients.py` (barrido SQL directo, sin
        pasar por este agregado)."""
        if self.is_expired(now):
            raise AuthorizationRequestExpiredError(f"solicitud {self.id} caducada")
