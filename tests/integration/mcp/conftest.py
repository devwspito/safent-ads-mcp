"""Fixtures compartidas de los bancos de contrato fake-vs-SQL de esta lane
(`wiring2`). Los puertos SQL (`SqlPortfolioReadPort` y hermanos) abren su
PROPIA sesion por llamada (`session_factory()`, mismo patron que
`SqlBrandReadPort`), asi que necesitan datos COMMITEADOS -- una sesion de
`rolled_back_session` (`tests.conftest`) quedaria invisible (mismo motivo
que documenta `tests/integration/mcp/test_sql_brand_read_port.py`).

`mcp_session_factory` es SINCRONA a proposito (solo construye el motor, no
ejecuta ninguna consulta): las fixtures parametrizadas fake/sql de cada
banco de contrato la piden con `request.getfixturevalue(...)` SOLO en la
rama `sql` (mismo patron que `tests/contracts/rules/conftest.py` con
`database_url`) -- si fuera una fixture async, `getfixturevalue` no puede
resolverla desde dentro de otra fixture async ya corriendo dentro de su
propio bucle de eventos ("Runner.run() cannot be called from a running
event loop"), y forzarla como parametro normal levantaria el contenedor de
Postgres incluso para el doble en memoria."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
def mcp_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)
