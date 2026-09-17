"""Bucle local (RFC 8252 SS7.3): `127.0.0.1`, `localhost`, `[::1]` y
ningun otro host -- ni un puerto distinto, ni uno que solo se le PAREZCA
(`127.0.0.1.evil.example`, `localhost.evil`, `127.0.0.2`).

Antes este conjunto vivia copiado cuatro veces con la MISMA intencion --
`mcp_oauth/domain/client.py::RedirectUri` (redirect_uri de un cliente
OAuth), `composition/settings.py::ApiSettings` (`ADS_PUBLIC_BASE_URL`),
`tools/first_run.py::_validate_public_base_url` (el mismo asistente, antes
de escribir el `.env`) y `broker/application/managed_oauth_connect.py`
(callback de un OAuth gestionado) -- revision de PR 44 (T049): una cuarta
copia sin `[::1]` (`tools/first_run.py`) dejaba el asistente rechazando
una URL que `ApiSettings` ya aceptaba al arrancar de verdad. Un solo
`import` no puede divergir asi."""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_http_origin(url: str) -> bool:
    """`True` solo para `http://` (nunca `https://`, que no necesita esta
    excepcion) a un host que pertenece EXACTAMENTE a `LOOPBACK_HOSTS`
    (`urlsplit().hostname` ya normaliza a minuscula y desnuda los
    corchetes de un literal IPv6). Una URL que ni siquiera parsea una
    autoridad (`ValueError` de `urlsplit`, p.ej. un `[::1` sin cerrar) da
    `False`, nunca deja escapar la excepcion -- no es de bucle local,
    simplemente."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme == "http" and hostname in LOOPBACK_HOSTS
