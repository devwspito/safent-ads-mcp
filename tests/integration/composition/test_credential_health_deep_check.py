"""`_check_credentials` (`composition/api.py`, tasks.md T126, threat-model.md
C-21): recuentos agregados sobre `credential_refs` real para `/api/v1/
health/deep` -- nunca expone identificadores, solo `status` + recuentos.

`isolated_database_url` (no la base compartida): esta cuenta es GLOBAL por
diseno (sin filtro de negocio), mismo criterio que
`tests/integration/orchestration/test_maintenance_step.py` para
`verify_decision_log_chain`. Las aserciones son en DELTA (antes/despues de
insertar), nunca en un recuento absoluto: otros bancos que corren sobre la
misma base aislada tambien pueden insertar `credential_refs` sanas."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _check_credentials
from safent_ads.composition.container import Container

pytestmark = pytest.mark.integration


async def test_reports_ok_when_nothing_new_is_unhealthy(isolated_database_url: str) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        before = await _check_credentials(container)

        assert before.ok == (before.unhealthy == 0)
    finally:
        await container.aclose()


async def test_degrades_when_a_credential_is_expired(isolated_database_url: str) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        before = await _check_credentials(container)

        credential_id = uuid.uuid4()
        async with container.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credential_refs (id, platform, alias, status) "
                    "VALUES (:id, 'google', :alias, 'EXPIRED')"
                ),
                {"id": credential_id, "alias": f"alias-{credential_id.hex[:12]}"},
            )

        after = await _check_credentials(container)

        assert after.ok is False
        assert after.unhealthy == before.unhealthy + 1
        assert after.total == before.total + 1
    finally:
        await container.aclose()


async def test_connected_credentials_do_not_count_as_unhealthy(
    isolated_database_url: str,
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        before = await _check_credentials(container)

        credential_id = uuid.uuid4()
        async with container.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credential_refs (id, platform, alias, status) "
                    "VALUES (:id, 'meta', :alias, 'CONNECTED')"
                ),
                {"id": credential_id, "alias": f"alias-{credential_id.hex[:12]}"},
            )

        after = await _check_credentials(container)

        assert after.unhealthy == before.unhealthy
        assert after.total == before.total + 1
    finally:
        await container.aclose()
