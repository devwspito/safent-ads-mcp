"""Errores de dominio de `mcp_oauth` (data-model.md, threat-model.md
C-36..C-58). Violaciones de un invariante de un agregado o value object;
la presentacion (T009) las mapea por tipo a codigos de error OAuth, nunca
por mensaje."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class InvalidRedirectUriError(DomainError):
    """`redirect_uri` fuera del conjunto cerrado de D-11: no es `http` ni
    `https` a `127.0.0.1`/`[::1]`/`localhost` (RFC 8252 §7.3, RFC 6749
    §3.1.2), o trae userinfo, fragmento, caracteres de control, una
    autoridad que no parsea o un puerto fuera de 1-65535."""


class TooManyRedirectUrisError(DomainError):
    """Un `OAuthClient` no admite mas de 5 `redirect_uri` registradas."""


class InvalidClientNameError(DomainError):
    """`client_name` vacio, mas largo que el CHECK de `oauth_clients`, o
    con caracteres que mienten sobre lo que se lee en la pantalla de
    consentimiento (threat-model.md C-70 pieza 3)."""


class PublicClientCannotHaveSecretError(DomainError):
    """`token_endpoint_auth_method = "none"` con `client_secret_hash` no nulo."""


class ConfidentialClientRequiresSecretError(DomainError):
    """Un metodo de autenticacion distinto de `none` sin secreto hasheado."""


class EmptyScopeSetError(DomainError):
    """Un `ScopeSet` vacio no autoriza nada: no es un estado valido."""


class UnknownScopeError(DomainError):
    """Un token de scope fuera de `ads:read`/`ads:propose`."""


class InvalidTokenHashError(DomainError):
    """La cadena no es un hash sha256 hexadecimal de 64 caracteres."""


class InvalidCodeChallengeError(DomainError):
    """`code_challenge` no tiene la forma base64url de EXACTAMENTE 43
    caracteres que produce un digest S256 (RFC 7636 §4.1-4.2,
    threat-model.md C-36) -- el rango 43-128 es el de `code_verifier`, no
    el del challenge."""


class AuthorizationRequestNotPendingError(DomainError):
    """Se intento consentir o denegar una solicitud que ya no esta PENDING."""


class AuthorizationRequestNotConsentedError(DomainError):
    """Se intento canjear el codigo de una solicitud que nunca se consintio."""


class AuthorizationRequestExpiredError(DomainError):
    """La solicitud (o su codigo) supero su TTL (data-model.md: 10 min /
    60 s)."""


class CodeAlreadyRedeemedError(DomainError):
    """El codigo de autorizacion ya se canjeo una vez (threat-model.md
    C-38): el replay revoca la concesion emitida desde el, no solo falla
    aqui."""


class UnknownTokenError(DomainError):
    """El hash presentado no pertenece a ningun `IssuedToken` de esta
    concesion, o no es del tipo esperado (acceso/refresco)."""


class TokenExpiredError(DomainError):
    """El token esta ACTIVE pero su `expires_at` ya paso."""


class RefreshTokenReusedError(DomainError):
    """Se presento un refresh token ya ROTATED o REVOKED (threat-model.md
    C-43): la concesion entera queda revocada como consecuencia."""


class GrantRevokedError(DomainError):
    """La concesion ya esta revocada: no puede rotar ni emitir tokens."""


class ScopeExpansionError(DomainError):
    """Un refresco pidio un alcance que no es subconjunto del concedido
    originalmente (data-model.md: "el alcance nunca crece al refrescar")."""
