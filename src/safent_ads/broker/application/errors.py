"""Excepciones de caso de uso de `broker/application` (shared/errors.py:
"Fallo de un caso de uso: entidad no encontrada, precondicion de puerto,
etc.")."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class OAuthSessionNotFoundError(ApplicationError):
    """`state` desconocido, ya consumido, o nunca emitido por `begin`."""


class OAuthSessionExpiredError(ApplicationError):
    """La sesion existia pero su TTL ya paso (contracts/rest-api.md: "TTL
    corto")."""


class OAuthProviderDeniedError(ApplicationError):
    """El proveedor rechazo el intercambio (codigo/verifier invalido,
    consentimiento retirado, etc.). Envuelve el error saneado del
    adaptador — nunca el mensaje crudo del proveedor."""


class CredentialNotFoundError(ApplicationError):
    """No hay credencial guardada para el `CredentialRefId` pedido."""


class GoogleProjectAccessDeniedError(OAuthProviderDeniedError):
    """Falta aprobar el proyecto Cloud para operar cuentas Google Ads reales."""


class GoogleAccountSelectionRequiredError(OAuthProviderDeniedError):
    """The user must select a customer before managed Google authorization."""


class GoogleAccountNotEnabledError(OAuthProviderDeniedError):
    """The selected Google Ads account is closed or not fully enabled."""


class GoogleAccountAccessDeniedError(OAuthProviderDeniedError):
    """Google denied access to the selected customer, not the OAuth consent."""


class AppCredentialsNotConfiguredError(ApplicationError):
    """Ni el almacen cifrado de credenciales de VENDOR ni `BrokerSettings`
    (respaldo de desarrollo) tienen nada guardado para esta plataforma --
    `oauth_begin`/`oauth_complete` fallan cerrado con esto en vez de
    construir una `authorization_url` rota con un `client_id` vacio."""


class AppCredentialsIncompleteError(ApplicationError):
    """El llamante pidio guardar credenciales de VENDOR sin todos los
    campos obligatorios de la plataforma -- defensa en profundidad: la
    validacion de forma/longitud real vive en `accounts/presentation`
    (REST), el unico camino de escritura hasta este `op`."""


class EnvelopeNotDeclaredError(ApplicationError):
    """`config/caps.yaml` no declara `panel_managed`: el panel no puede
    fijar ningun tope (spec 008 §5). Es el comportamiento por defecto y el
    de siempre, no un fallo de configuracion."""


class EnvelopeExceededError(ApplicationError):
    """Algun importe pedido supera el sobre declarado por el operador."""


class EnvelopeAccountsExhaustedError(ApplicationError):
    """Ya hay `envelope.max_accounts` cuentas con tope del panel."""


class EnvelopeChangesExhaustedError(ApplicationError):
    """Se agoto `envelope.max_cap_changes_per_day` del dia natural (UTC).
    Sin este contador, un `ads-api` comprometido puede oscilar los topes
    sin fin dentro del sobre y ahogar la auditoria en ruido."""


class InvalidAccountCapsError(ApplicationError):
    """El cuerpo pedido no es un tope valido: importes incoherentes, divisa
    distinta de la del sobre, o un campo que el panel no fija."""
