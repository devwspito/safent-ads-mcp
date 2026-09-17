"""Comparacion de bearer en tiempo constante, usada por
`mcp_oauth.presentation.token_verifier.CompositeTokenVerifier` (rama
estatica) para autenticar tanto `/mcp` como `GET /mcp/health`: el mismo
secreto (`ApiSettings.mcp_token`, `ADS_MCP_TOKEN`) nunca se compara dos
veces con dos implementaciones distintas (threat-model.md C-9, C-48). Vive
en `shared/` (tasks.md 002 T001) para que `mcp_oauth` tambien lo use sin
crear un ciclo `mcp_oauth -> mcp`."""

from __future__ import annotations

import hmac

_BEARER_SCHEME = "bearer"


def extract_bearer_token(authorization_header: str) -> str | None:
    """RFC 6749 SS7.1: el nombre del esquema (`Bearer`) no distingue
    mayusculas/minusculas -- M1 de la revision de seguridad (16-sep): el
    SDK ya acepta `bearer`/`BEARER`/... en `BearerAuthBackend.authenticate`
    (`.lower().startswith("bearer ")`, protege `/mcp`); esta funcion
    protege `GET /mcp/health` con el MISMO criterio, o un cliente que manda
    el esquema en minuscula pasaria en uno y no en el otro."""
    scheme, separator, token = authorization_header.partition(" ")
    if scheme.lower() != _BEARER_SCHEME or not separator or not token:
        return None
    return token


def is_token_valid(token: str, expected_token: str) -> bool:
    """Comparacion en tiempo constante del token ya pelado (sin el
    esquema): la usa `mcp_oauth/presentation/token_verifier.py::
    CompositeTokenVerifier` (recibe el token ya pelado por
    `BearerAuthBackend` del SDK) -- una unica implementacion de la
    comprobacion (threat-model.md C-9).

    Se compara en BYTES, no en `str`: `hmac.compare_digest` sobre cadenas
    exige que las dos sean ASCII puro y, si no, lanza `TypeError`. El
    token llega de una cabecera HTTP, o sea de fuera, asi que un
    `Authorization: Bearer ñ` convertia una credencial invalida -- un 401
    de manual -- en una excepcion no capturada y un 500. Un 500 ademas
    distingue: dice al que prueba que ese byte llego mas lejos que los
    otros. `surrogatepass` para que un sustituto suelto tampoco reviente:
    aqui nada se descodifica, solo se compara."""
    return hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"),
        expected_token.encode("utf-8", "surrogatepass"),
    )
