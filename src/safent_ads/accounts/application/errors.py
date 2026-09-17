"""Excepciones de caso de uso de `accounts` (shared/errors.py: fallo de
un caso de uso -> entidad no encontrada, precondicion de puerto, etc.)."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class AccountNotFoundError(ApplicationError):
    """No existe `PlatformAccount` para el `AccountRef` pedido."""


class EntityNotFoundError(ApplicationError):
    """No existe `AdEntity` para el `EntityRef` pedido."""


class OAuthSessionNotFoundError(ApplicationError):
    """`state` desconocido, ya consumido, o nunca emitido por
    `BeginOAuthConnect` (contracts/rest-api.md: "un solo uso")."""


class OAuthSessionExpiredError(ApplicationError):
    """La sesion OAuth existia pero su TTL ya paso."""


class OAuthProviderDeniedError(ApplicationError):
    """El broker rechazo el intercambio (proveedor, `code`/`state`
    invalido...). Envuelve el `error_code` del broker, nunca su detalle
    crudo."""


class CredentialNotFoundError(ApplicationError):
    """No hay `PlatformCredential` para el `CredentialRefId` pedido."""


class BrokerRequestDeniedError(ApplicationError):
    """`OAuthBrokerPort`/`AdsPlatformPort` respondieron `{"ok": false}`.
    Parte del contrato del puerto -- toda implementacion de
    `accounts/infrastructure/*_client.py` la lanza en vez de una propia,
    para que quien llama al puerto (aplicacion) nunca dependa de un tipo de
    infraestructura."""

    def __init__(self, error_code: str, reason: str | None = None) -> None:
        super().__init__(f"{error_code}: {reason}" if reason else error_code)
        self.error_code = error_code
        self.reason = reason


class AdEntityParentNotFoundError(ApplicationError):
    """El padre declarado por una `AdEntity` no existe en `ad_entities` ni en
    `platform_accounts`: guardarla dejaria la jerarquia rota. Parte del
    contrato de `AdEntityRepository.save` (mismo criterio que
    `BrokerRequestDeniedError`): `accounts/infrastructure/sql_repositories.py`
    la lanza en vez de una propia, para que quien llama al puerto
    (aplicacion, incluida `packages` -- 003-entidades-creadas) nunca dependa
    de un tipo de infraestructura."""
