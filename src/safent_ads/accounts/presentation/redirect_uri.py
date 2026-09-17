"""`platform_app_redirect_uri`: el único constructor de la URI de callback
OAuth por plataforma, compartido por `connections_router.py`
(`POST .../reconnect/start`, quien la envía al proveedor) y
`platform_apps_router.py` (`GET`/`PUT /platform-apps`, quien la muestra al
propietario para que la registre en la consola del proveedor) -- una sola
función para que las dos rutas nunca puedan divergir sobre qué URI exacta
hay que dar de alta en Google Cloud Console / la app de Meta.

Dentro de Safent (companion mode), el navegador nunca resuelve
`settings.public_base_url` (`https://ads.safent.internal:8443`, host
inexistente para la webview): solo llega a `ads-api` a traves del puente
same-origin `ads_bridge` (lumen-runtime-next, `/ads/{path}`). `request`
opcional deja que las dos rutas deriven el origen real del navegador de los
`X-Forwarded-*` que ese puente añade -- solo se confian dentro de
`ADS_COMPANION_MODE=true` (mismo criterio que `ForwardedPrefixMiddleware`,
`composition/api.py`: fuera de compose companion, `ads-api` nunca esta
detras del puente, asi que confiar en esas cabeceras de un cliente
cualquiera seria un vector host-header/open-redirect)."""

from __future__ import annotations

from fastapi import Request

from safent_ads.composition.settings import ApiSettings
from safent_ads.shared.ids import PlatformCode

_FORWARDED_HOST_HEADER = "x-forwarded-host"
_FORWARDED_PROTO_HEADER = "x-forwarded-proto"
_FORWARDED_PREFIX_HEADER = "x-forwarded-prefix"
# Mismo allow-list que `ForwardedPrefixMiddleware` (composition/api.py) --
# nunca se propaga un prefijo arbitrario a una URL que se envia al proveedor.
_ALLOWED_FORWARDED_PREFIXES = frozenset({"", "/ads"})


def platform_app_redirect_uri(
    settings: ApiSettings, provider: PlatformCode, request: Request | None = None
) -> str:
    origin = _forwarded_browser_origin(settings, request) if request is not None else None
    base = origin if origin is not None else settings.public_base_url
    return f"{base}/api/v1/platform-accounts/{provider.value}/reconnect/callback"


def _forwarded_browser_origin(settings: ApiSettings, request: Request) -> str | None:
    if not settings.companion_mode:
        return None
    host = request.headers.get(_FORWARDED_HOST_HEADER)
    if not host:
        return None
    scheme = request.headers.get(_FORWARDED_PROTO_HEADER, "https")
    prefix = request.headers.get(_FORWARDED_PREFIX_HEADER, "")
    if prefix not in _ALLOWED_FORWARDED_PREFIXES:
        prefix = ""
    return f"{scheme}://{host}{prefix}"
