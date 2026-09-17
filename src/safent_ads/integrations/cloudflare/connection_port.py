"""Puerto de aplicacion de la conexion Cloudflare gestionada desde el panel
(lane 006-cloudflare-ui, owner decision: "crear una conexion con
Cloudflare pidiendo el token e indicando el enlace donde crearlo").

Separado de `port.py` (contrato de LECTURA/ESCRITURA de DNS que ya
consume `mcp/presentation/cloudflare_tools.py`) a proposito -- este es el
contrato de la conexion en si (guardar/leer/borrar el token), que
`rest.py` (panel) y `connection_service.py` consumen. `CloudflareConnectionRecord.
token` es el UNICO punto de todo este carril que lleva el secreto en
claro en memoria -- nunca cruza a `CloudflareConnectionStatus` (lo que ve
el panel/MCP) ni a un log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.shared.errors import ApplicationError


@dataclass(frozen=True, slots=True)
class CloudflareConnectionRecord:
    """Fila ya descifrada de `cloudflare_connection` (0050). Vive solo
    dentro de `integrations/cloudflare/` -- `rest.py` la consume una vez
    y construye `CloudflareConnectionStatus` (sin token) para el panel."""

    token: str
    account_id: str | None
    zones: tuple[str, ...]
    connected_at: datetime
    connected_by_owner_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class CloudflareConnectionStatus:
    """Lo que ve el panel/MCP: nunca el token. `GET /api/v1/integrations/
    cloudflare` y la herramienta `get_cloudflare_connection_status` sirven
    exactamente esta forma."""

    connected: bool
    account_id: str | None
    zones: tuple[str, ...]
    connected_at: datetime | None
    create_token_url: str
    required_permissions: tuple[str, ...]


class CloudflareConnectionStore(Protocol):
    """`SqlCloudflareConnectionStore` (infra) es la unica implementacion
    real; los tests de `connection_service.py` usan un doble en memoria."""

    async def get(self) -> CloudflareConnectionRecord | None: ...

    async def save(self, record: CloudflareConnectionRecord) -> None: ...

    async def delete(self) -> None: ...


class CloudflareTokenInvalidError(ApplicationError):
    """Cloudflare rechazo el token (`verify`) o el token no puede leer
    zonas (`GET /zones`): la conexion nunca se guarda. El mensaje es
    siempre propio -- nunca el cuerpo crudo de Cloudflare ni el token
    (mismo criterio que `CloudflareApiError`)."""
