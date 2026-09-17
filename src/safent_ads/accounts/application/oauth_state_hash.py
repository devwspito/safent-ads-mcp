"""`hash_state`: mismo algoritmo (sha256 hex) que
`broker/infrastructure/oauth_state.py::hash_state` y que
`iam.application.verify_totp::hash_session_token` -- el `state` se guarda
hasheado, como el token de sesion (contracts/rest-api.md §Conexiones).

Duplicado a proposito en vez de importado desde `broker/`: son ~3 lineas,
y `ads-api` no debe depender de codigo del proceso `ads-broker`
(`accounts/infrastructure/broker_client.py` documenta la misma decision
para el framing del socket)."""

from __future__ import annotations

import hashlib


def hash_state(raw_state: str) -> str:
    return hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
