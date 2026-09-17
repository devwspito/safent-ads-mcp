"""Errores de aplicacion de `mcp_oauth`: entidad no encontrada o
precondicion de politica incumplida. La presentacion (T009) los mapea por
tipo a codigos de error OAuth (`invalid_client`, `invalid_grant`,
`invalid_target`...), nunca por mensaje."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class UnknownClientError(ApplicationError):
    """`client_id` no registrado."""


class RedirectUriMismatchError(ApplicationError):
    """`redirect_uri` no coincide con ninguna registrada para el cliente,
    o no coincide con la de la solicitud que origino el codigo."""


class InvalidTargetError(ApplicationError):
    """`resource` distinto del recurso canonico (RFC 8707, threat-model.md
    C-46)."""


class TooManyUnconsentedClientsError(ApplicationError):
    """Tope de clientes sin consentir alcanzado (threat-model.md C-42)."""


class TooManyPendingAuthorizationsError(ApplicationError):
    """Tope de solicitudes PENDING por cliente alcanzado (threat-model.md
    C-58)."""


class AuthorizationRequestNotFoundError(ApplicationError):
    """El `txn_id` no existe."""


class ClientMismatchError(ApplicationError):
    """El `client_id` presentado no es el dueno del codigo o la concesion."""


class UnknownAuthorizationCodeError(ApplicationError):
    """El codigo de autorizacion no corresponde a ninguna solicitud."""


class UnknownRefreshTokenError(ApplicationError):
    """El refresh token no corresponde a ninguna concesion viva."""


class PkceVerificationError(ApplicationError):
    """El `code_verifier` no reproduce el `code_challenge` guardado."""


class GrantNotFoundError(ApplicationError):
    """La concesion no existe o no pertenece al propietario que la pide --
    mismo error para ambos casos: opacidad ante IDOR."""


class ConcurrentRefreshInProgressError(ApplicationError):
    """Otra transaccion esta rotando ESTE MISMO refresh token ahora mismo
    (fix/refresh-rotation-race, C-43): dos refrescos concurrentes y
    legitimos del mismo token, no un reuso -- la ganadora de esa otra
    rotacion sigue viva, asi que esta llamada se rechaza limpia
    (`invalid_grant`) SIN revocar la familia. Distinto de
    `RefreshTokenReusedError` (dominio): ese es el refresh ya rotado y
    asentado, sin nadie corriendo a la vez -- reuso genuino, revoca."""
