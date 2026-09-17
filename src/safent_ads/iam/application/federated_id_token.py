"""Validacion de las claims del `id_token` (002b research.md Decision D).

Funcion PURA: entra un diccionario de claims, sale una identidad validada o
una excepcion. Sin httpx, sin pydantic, sin FastAPI y sin reloj propio -- el
transporte (canje del `code`, egreso fijado a Google) vive en
`iam/infrastructure/google_oidc_provider.py`, que solo delega aqui.

La firma del `id_token` NO se re-verifica contra el JWKS de Google: llega por
el canal TLS directo con el token endpoint en respuesta a NUESTRO canje (OIDC
Core §3.1.3.7 lo permite en el code flow). Lo que si se comprueba, en orden
fail-closed, es que la identidad venga del emisor esperado, fuera emitida para
ESTA instalacion (`aud`) y para ESTA peticion (`nonce`), y siga viva (`exp`).

Ningun mensaje de error lleva valores: ni el nonce, ni el `aud`, ni el token
(FR-118, SC-107). El correo tampoco aparece en los mensajes; su tratamiento
esta limitado a autenticacion y auditoria (NFR-107)."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from safent_ads.iam.application.ports import FederatedIdentityClaims
from safent_ads.iam.domain.email import Email, InvalidEmailError
from safent_ads.iam.domain.errors import (
    InvalidFederatedSubjectError,
    UnknownFederatedIssuerError,
)
from safent_ads.iam.domain.federated_identity import FederatedIssuer, FederatedSubject
from safent_ads.iam.domain.federated_transaction import ReferenceHash
from safent_ads.shared.errors import ApplicationError


class FederatedIdTokenInvalidError(ApplicationError):
    """El `id_token` no es utilizable: emisor, audiencia, caducidad, nonce o
    identidad que no cuadran. Vive aqui, junto a su unico lanzador, igual que
    `InvalidEmailError` vive en `domain/email.py`."""


def validate_id_token_claims(
    claims: Mapping[str, Any],
    *,
    expected_audience: str,
    expected_nonce_hash: ReferenceHash,
    now: datetime,
) -> FederatedIdentityClaims:
    issuer = _require_issuer(claims.get("iss"))
    _require_audience(claims.get("aud"), expected_audience)
    _require_azp_if_present(claims.get("azp"), expected_audience)
    _require_unexpired(claims.get("exp"), now)
    _require_nonce(claims.get("nonce"), expected_nonce_hash)
    return FederatedIdentityClaims(
        issuer=issuer,
        subject=_require_subject(claims.get("sub")),
        email=_require_email(claims.get("email")),
        email_verified=_is_verified(claims.get("email_verified")),
    )


def _require_issuer(raw: Any) -> FederatedIssuer:  # noqa: ANN401 - claim heterogenea
    if not isinstance(raw, str):
        raise FederatedIdTokenInvalidError("id_token sin issuer")
    try:
        return FederatedIssuer.parse(raw)
    except UnknownFederatedIssuerError as exc:
        raise FederatedIdTokenInvalidError("issuer del id_token inesperado") from exc


def _require_audience(raw: Any, expected: str) -> None:  # noqa: ANN401 - claim heterogenea
    if not isinstance(raw, str) or not hmac.compare_digest(raw, expected):
        raise FederatedIdTokenInvalidError("audience del id_token no coincide")


def _require_azp_if_present(raw: Any, expected: str) -> None:  # noqa: ANN401 - claim heterogenea
    """threat-model.md C-67/MENOR-1: `azp` (parte autorizada) es opcional en
    OIDC -- Google solo la manda cuando `aud` tiene mas de un valor -- pero
    si viene, tiene que ser ESTA instalacion. Ausente ⇒ se acepta igual
    (nada que comprobar); presente y distinta ⇒ rechazo, mismo criterio de
    comparacion en tiempo constante que `aud` y `nonce`."""
    if raw is None:
        return
    if not isinstance(raw, str) or not hmac.compare_digest(raw, expected):
        raise FederatedIdTokenInvalidError("azp del id_token no coincide")


def _require_unexpired(raw: Any, now: datetime) -> None:  # noqa: ANN401 - claim heterogenea
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise FederatedIdTokenInvalidError("id_token sin exp valido")
    if now >= datetime.fromtimestamp(raw, tz=UTC):
        raise FederatedIdTokenInvalidError("id_token caducado")


def _require_nonce(raw: Any, expected: ReferenceHash) -> None:  # noqa: ANN401 - heterogenea
    if not isinstance(raw, str):
        raise FederatedIdTokenInvalidError("id_token sin nonce")
    if not hmac.compare_digest(ReferenceHash.of(raw).value, expected.value):
        raise FederatedIdTokenInvalidError("nonce del id_token no coincide")


def _require_subject(raw: Any) -> FederatedSubject:  # noqa: ANN401 - claim heterogenea
    if not isinstance(raw, str):
        raise FederatedIdTokenInvalidError("id_token sin sub")
    try:
        return FederatedSubject(raw)
    except InvalidFederatedSubjectError as exc:
        raise FederatedIdTokenInvalidError("sub del id_token invalido") from exc


def _require_email(raw: Any) -> Email:  # noqa: ANN401 - claim heterogenea
    if not isinstance(raw, str):
        raise FederatedIdTokenInvalidError("id_token sin email")
    try:
        return Email(raw)
    except InvalidEmailError as exc:
        raise FederatedIdTokenInvalidError("email del id_token invalido") from exc


def _is_verified(raw: Any) -> bool:  # noqa: ANN401 - claim heterogenea
    """Google manda `email_verified` como booleano o como la cadena "true".
    Cualquier otra cosa (incluida su ausencia) NO es verificacion: el correo
    verificado es condicion necesaria para crear sesion (FR-102)."""
    return raw is True or (isinstance(raw, str) and raw.strip().lower() == "true")
