"""Constantes de politica de `mcp_oauth` (tasks.md T005, threat-model.md
§10). Viven aqui como constantes de codigo, no de entorno -- mismo motivo
que `iam/application/session_policy.py`: cambiarlos exige un despliegue,
lo cual es deliberado para una politica de seguridad (evita downgrades por
variable de entorno mal puesta)."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.mcp_oauth.domain.scope import Scope, ScopeSet

AUTHORIZATION_REQUEST_TTL = timedelta(minutes=10)
AUTHORIZATION_CODE_TTL = timedelta(seconds=60)
ACCESS_TOKEN_TTL = timedelta(minutes=60)
REFRESH_TOKEN_TTL = timedelta(days=30)
UNCONSENTED_CLIENT_TTL = timedelta(hours=24)
MAX_UNCONSENTED_CLIENTS = 50
# M3 de la revision de seguridad (16-sep): al llegar a `MAX_UNCONSENTED_CLIENTS`,
# `RegisterClient` desaloja el cliente sin consentir MAS ANTIGUO (CASCADE lo
# hace seguro, threat-model.md C-42) en vez de rechazar el registro -- DCR
# sigue abierta a Internet sin convertirse en un cerrojo permanente para un
# atacante que rellena el cupo. Este techo es el freno DE VERDAD: si el
# desalojo no pudiera seguir el ritmo (bug, Postgres caido a medias), 500
# clientes sin consentir siguen siendo un rechazo explicito, no una tabla
# sin fondo.
MAX_UNCONSENTED_CLIENTS_HARD_CEILING = MAX_UNCONSENTED_CLIENTS * 10
MAX_PENDING_PER_CLIENT = 5
# Nit de la revision de seguridad (16-sep): un `ScopeSet` construido
# directamente sobre el enum, no `ScopeSet.parse("ads:read")` -- evita la
# cadena magica repetida (L2, `presentation/sdk_provider.py::_parse_scope`)
# y no puede desalinearse de `Scope.READ` si el valor del enum cambia.
DEFAULT_DCR_SCOPE = ScopeSet(frozenset({Scope.READ}))
