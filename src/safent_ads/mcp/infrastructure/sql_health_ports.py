"""Adaptadores SQL de `mcp.application.health` (`GET /mcp/health`): mismo
patron de sesion por llamada que `sql_business_directory.SqlBusinessDirectory`/
`sql_brand_read_port.SqlBrandReadPort`.

Los dos `except Exception` son deliberados, igual criterio que
`composition/api.py::_check_database`/`_check_broker_socket`: un health
check nunca debe propagar una traza ni un error tecnico al llamante --
degradar a `False`/conjunto vacio es la respuesta correcta a una BD
inalcanzable."""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_SELECT_LINKED_PLATFORMS = text("SELECT DISTINCT platform FROM platform_accounts")
_SELECT_ONE = text("SELECT 1")


class SqlAccountLinkStatusPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def linked_platforms(self) -> frozenset[str]:
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(_SELECT_LINKED_PLATFORMS)).all()
        except Exception:  # noqa: BLE001 - un health check nunca debe propagar
            logger.warning("mcp_health_accounts_unreachable")
            return frozenset()
        return frozenset(row.platform for row in rows)


class SqlDatabaseHealthPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ping(self) -> bool:
        try:
            async with self._session_factory() as session:
                await session.execute(_SELECT_ONE)
        except Exception:  # noqa: BLE001 - un health check nunca debe propagar
            logger.warning("mcp_health_db_unreachable")
            return False
        return True
