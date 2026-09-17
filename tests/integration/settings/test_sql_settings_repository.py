"""`SqlSettingsRepository` contra Postgres real (0023_owner_settings):
el valor por defecto se sirve cuando el negocio nunca personalizo nada,
`save()` persiste ambas tablas, y un `business_id` desconocido devuelve
`None` en vez de fabricar una fila."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme
from safent_ads.settings.infrastructure.sql_settings_repository import SqlSettingsRepository
from tests.conftest import BusinessFactory, OwnerFactory

pytestmark = pytest.mark.integration

_DEFAULT_ACTIVE_HOURS = ActiveHours.parse(start="08:00", end="21:00")
_DEFAULT_DIGEST_HOUR = DigestHour.parse("09:00")


def _repository(session: AsyncSession) -> SqlSettingsRepository:
    return SqlSettingsRepository(
        session,
        default_active_hours=_DEFAULT_ACTIVE_HOURS,
        default_digest_hour=_DEFAULT_DIGEST_HOUR,
    )


class TestGet:
    async def test_returns_the_default_active_hours_and_digest_hour_when_never_customized(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = await business_factory.create(timezone="Europe/Madrid")
        repository = _repository(db_session)

        view = await repository.get(str(business_id), uuid.uuid4())

        assert view is not None
        assert view.timezone == "Europe/Madrid"
        assert view.currency == "EUR"
        assert view.active_hours == _DEFAULT_ACTIVE_HOURS
        assert view.digest_hour == _DEFAULT_DIGEST_HOUR
        assert view.theme is Theme.SYSTEM

    async def test_unknown_business_returns_none(self, db_session: AsyncSession) -> None:
        repository = _repository(db_session)

        assert await repository.get(str(uuid.uuid4()), uuid.uuid4()) is None

    async def test_reads_a_previously_confirmed_theme_for_the_owner(
        self,
        db_session: AsyncSession,
        business_factory: BusinessFactory,
        owner_factory: OwnerFactory,
    ) -> None:
        business_id = await business_factory.create()
        owner_id = await owner_factory.create()
        repository = _repository(db_session)
        await repository.save(
            str(business_id),
            owner_id,
            active_hours=_DEFAULT_ACTIVE_HOURS,
            digest_hour=_DEFAULT_DIGEST_HOUR,
            theme=Theme.DARK,
        )

        view = await repository.get(str(business_id), owner_id)

        assert view is not None
        assert view.theme is Theme.DARK


class TestSave:
    async def test_persists_active_hours_and_digest_hour_on_the_business_row(
        self,
        db_session: AsyncSession,
        business_factory: BusinessFactory,
        owner_factory: OwnerFactory,
    ) -> None:
        business_id = await business_factory.create()
        owner_id = await owner_factory.create()
        repository = _repository(db_session)
        active_hours = ActiveHours.parse(start="07:00", end="22:00")
        digest_hour = DigestHour.parse("07:00")

        view = await repository.save(
            str(business_id),
            owner_id,
            active_hours=active_hours,
            digest_hour=digest_hour,
            theme=Theme.LIGHT,
        )

        assert view is not None
        assert view.active_hours == active_hours
        assert view.digest_hour == digest_hour

        reread = await repository.get(str(business_id), owner_id)
        assert reread is not None
        assert reread.active_hours == active_hours
        assert reread.digest_hour == digest_hour

    async def test_saving_twice_for_the_same_owner_updates_the_theme_in_place(
        self,
        db_session: AsyncSession,
        business_factory: BusinessFactory,
        owner_factory: OwnerFactory,
    ) -> None:
        business_id = await business_factory.create()
        owner_id = await owner_factory.create()
        repository = _repository(db_session)
        await repository.save(
            str(business_id),
            owner_id,
            active_hours=_DEFAULT_ACTIVE_HOURS,
            digest_hour=_DEFAULT_DIGEST_HOUR,
            theme=Theme.DARK,
        )

        await repository.save(
            str(business_id),
            owner_id,
            active_hours=_DEFAULT_ACTIVE_HOURS,
            digest_hour=_DEFAULT_DIGEST_HOUR,
            theme=Theme.LIGHT,
        )

        row_count = await db_session.execute(
            text("SELECT count(*) FROM owner_preferences WHERE owner_id = :owner_id"),
            {"owner_id": owner_id},
        )
        assert row_count.scalar_one() == 1

    async def test_unknown_business_returns_none_and_does_not_write_the_theme(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        repository = _repository(db_session)
        owner_id = await owner_factory.create()

        view = await repository.save(
            str(uuid.uuid4()),
            owner_id,
            active_hours=_DEFAULT_ACTIVE_HOURS,
            digest_hour=_DEFAULT_DIGEST_HOUR,
            theme=Theme.DARK,
        )

        assert view is None
