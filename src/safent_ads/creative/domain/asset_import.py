"""Reglas puras de `import_creative_asset` (tool-surface.md §2.2 P1,
threat-model.md C-11/C-12: "SSRF por argumentos de tool (`url`,
`image_url`)"). `import_creative_asset` es la unica herramienta de
`creative` que acepta una URL en un argumento — es literalmente su
proposito, traer el activo que una herramienta nativa del agente acaba de
producir — por eso necesita su propio allow-list de host exacto, en vez de
la regla general "sin URLs libres" de `presentation/mcp_tools.py`.

Solo host + esquema: el bloqueo de IP privada/loopback/link-local tras la
resolucion DNS es infraestructura (`infrastructure/http_asset_fetcher.py`)
porque exige I/O real. `creative` no puede importar
`broker/infrastructure/egress_guard.py` sin invertir el grafo de
dependencias entre contextos acotados (plan.md §4), asi que el mismo
principio se repite aqui a proposito, no se comparte."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_ALLOWED_SCHEME = "https"


class SourceUrlNotAllowedError(ValueError):
    """La URL de origen no supera el allow-list de import (C-11/C-12)."""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def validate_import_source_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Devuelve el host en minusculas si `url` supera el allow-list;
    lanza `SourceUrlNotAllowedError` en cualquier otro caso. Nunca resuelve
    DNS — eso es responsabilidad de la infraestructura, que debe llamar
    esto ANTES de abrir ninguna conexion (fail closed)."""
    parts = urlsplit(url)
    if parts.scheme != _ALLOWED_SCHEME:
        raise SourceUrlNotAllowedError(f"esquema no permitido, se exige https: {url!r}")
    host = parts.hostname
    if not host:
        raise SourceUrlNotAllowedError(f"URL sin host: {url!r}")
    if _is_ip_literal(host):
        raise SourceUrlNotAllowedError(f"host como literal IP no permitido: {host!r}")
    normalized = host.lower()
    if normalized not in allowed_hosts:
        raise SourceUrlNotAllowedError(f"host fuera de la lista blanca de import: {host!r}")
    return normalized
