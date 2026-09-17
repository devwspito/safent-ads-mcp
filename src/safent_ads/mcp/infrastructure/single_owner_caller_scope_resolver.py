"""Resolutor de alcance del modo de un solo propietario (`ADS_SINGLE_OWNER_
MODE=true`): el Safent local del dueno (motor Hermes), un modo de primera
clase -- no un flag de desarrollo (aclaracion del dueno, 004 tasks.md
A6/A10). Sustituye a `StaticCallerScopeResolver` (borrado en A1): nunca usa
`None` como "todos los negocios" -- enumera los negocios activos de la
base en cada resolucion, un alcance explicito y acotado, nunca implicito.

El contrato de `/mcp` que el motor Hermes ya conoce no cambia: mismo path,
misma cabecera `Authorization: Bearer <ADS_MCP_TOKEN>`. Lo unico nuevo es
que el alcance ya no es un `None` implicito, sino un `CallerScope` real con
permiso `aprobar` (`registries_by_permission` le sirve el mismo catalogo
completo que antes)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.caller_scope import OWNER_CALLER_ID, CallerScope, Permission
from safent_ads.mcp.application.seat_authority import SeatAuthorityDeniedError
from safent_ads.mcp.infrastructure.sql_active_business_ids import SqlActiveBusinessIds
from safent_ads.shared.bearer import is_token_valid

_OWNER_CALLER_ID = OWNER_CALLER_ID
_OWNER_LABEL = "Dueño"


class SingleOwnerCallerScopeResolver:
    """Compara el bearer estatico (`ADS_MCP_TOKEN`) en tiempo constante y
    concede permiso `aprobar` sobre los negocios activos de la base --
    mismo comportamiento practico que F1 (un unico propietario), pero el
    alcance se calcula en cada llamada, nunca se supone `None`."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, expected_token: str | None
    ) -> None:
        self._active_business_ids = SqlActiveBusinessIds(session_factory)
        self._expected_token = expected_token

    async def resolve(self, bearer_token: str) -> CallerScope:
        # Vacio o ausente = no hay bearer estatico configurado: sin puerta
        # que abrir no se compara nada y se deniega. `None` es
        # `ADS_MCP_TOKEN` sin definir; la cadena VACIA es `ADS_MCP_TOKEN=`
        # a secas en el fichero de entorno, que llega hasta aqui como un
        # `SecretStr("")` y abriria la puerta a cualquiera que mandase un
        # bearer vacio. Las dos son "sin configurar", y default-deny las
        # resuelve igual (spec 008 fase E, seguimiento de T022).
        if not self._expected_token:
            raise SeatAuthorityDeniedError("ads_seat_invalid")
        if not is_token_valid(bearer_token, self._expected_token):
            raise SeatAuthorityDeniedError("ads_seat_invalid")
        return CallerScope(
            caller_id=_OWNER_CALLER_ID,
            allowed_business_ids=await self._active_business_ids.active_business_ids(),
            permission=Permission.APPROVE,
            person_label=_OWNER_LABEL,
        )
