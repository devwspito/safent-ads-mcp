"""`call_broker`: punto UNICO de traduccion de un fallo del broker
(incidente de produccion, companion 0.2.21: `list_meta_pages`/`list_google_
conversion_actions` dejaban escapar `BrokerRequestDeniedError`/
`BrokerConnectionError` crudos, que el SDK MCP convierte en el
`UnexpectedToolError` opaco -- nunca el sobre limpio del contrato). Todo
puerto de `mcp/infrastructure` que llame al broker por `BrokerSocketClient`
pasa el `Awaitable` por aqui en vez de duplicar la traduccion (mismo
criterio que el guard anti-SSRF compartido: una unica implementacion,
nunca copiada). Un codigo sin traduccion conocida se relanza tal cual --
`mount.py` lo convierte en `TOOL_FAILED`, nunca en un crash sin envolver."""

from __future__ import annotations

from collections.abc import Awaitable

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.mcp.application.errors import broker_denial_error, broker_unavailable_error

__all__ = ["call_broker"]


async def call_broker[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except BrokerRequestDeniedError as exc:
        translated = broker_denial_error(exc.error_code)
        if translated is None:
            raise
        raise translated from None
    except BrokerConnectionError as exc:
        raise broker_unavailable_error() from exc
