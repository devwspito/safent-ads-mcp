"""IP real del llamador detras de un proxy de confianza (threat-model.md
C-42/C-51, H1 16-sep; C-76/C-79, code review 17-sep).

Antes esta resolucion vivia dos veces con dos criterios distintos:
`composition/api.py::_make_forwarded_for_rate_limit_key` (sobre el `Scope`
ASGI crudo, honrando `ADS_TRUSTED_PROXY_HOPS`, para el limite de tasa) y una
copia mas simple -- sin honrar los saltos de confianza y sin validar que el
resultado fuera una IP de verdad -- repetida en `iam/presentation/{router,
reauth,federated_router}.py::_client_ip` (para el registro de intentos de
login y el tope de C-79). Esa copia devolvia el literal `"unknown"` cuando
`request.client` era `None`, y ese literal llegaba tal cual a una columna
`INET` -- `DataError`, 500, en la unica ruta anonima del login federado.

Funcion pura, sin FastAPI, sin Starlette, sin ASGI: cada llamador extrae
`forwarded_for`/`peer_ip` de su propio objeto de peticion (`Scope` o
`Request`) y le pasa las dos cadenas."""

from __future__ import annotations

import ipaddress


def resolve_client_ip(
    *, forwarded_for: str | None, peer_ip: str | None, trusted_proxy_hops: int
) -> str | None:
    """Sin proxy de confianza (`trusted_proxy_hops=0`, `compose.companion.yaml`,
    Safent conecta directo), `X-Forwarded-For` NUNCA se lee -- cualquier
    cliente puede escribirla. Con uno o mas saltos de confianza, se confia
    el valor MAS A LA DERECHA que ese ultimo proxy añadio el mismo -- los
    de su izquierda los pudo escribir cualquiera hablando con el proxy.

    Cabecera ausente, con menos valores de los saltos esperados, o con un
    valor que no parsea como IP real -> cae a la IP del socket. Ni una ni
    otra parsean como IP -> `None`, nunca un marcador de texto: la unica
    columna que persiste esto (`federated_login_transactions.ip_address`,
    `login_attempts.ip_address`) es `INET` y nullable a proposito para
    este caso -- un texto como `"unknown"` la revienta con un `DataError`."""
    if trusted_proxy_hops > 0 and forwarded_for:
        candidate = _forwarded_for_at_hop(forwarded_for, trusted_proxy_hops)
        if candidate is not None and _is_valid_ip(candidate):
            return candidate
    if peer_ip is not None and _is_valid_ip(peer_ip):
        return peer_ip
    return None


def _forwarded_for_at_hop(forwarded_for: str, trusted_proxy_hops: int) -> str | None:
    """El N-esimo valor contando desde la derecha (1-indexado): con
    `trusted_proxy_hops=1` es el ultimo valor, el que ese proxy añadio el
    mismo. Menos valores de los esperados (cabecera ausente, truncada o
    falseada con menos comas de las esperadas) devuelve `None`."""
    candidates = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(candidates) < trusted_proxy_hops:
        return None
    return candidates[-trusted_proxy_hops]


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
