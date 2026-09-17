"""`SettingsRepository` sobre `businesses`/`owner_preferences`
(0023_owner_settings). `active_hours_start`/`active_hours_end`/
`digest_hour` a NULL (negocio nunca personalizado) devuelven el valor por
defecto que trae el constructor -- el mismo `ADS_ACTIVE_HOURS`/`ADS_TZ` que
hoy usa `ads-worker` (`composition/settings.py`), para que el panel nunca
muestre un valor distinto del que el motor aplica de verdad.

`SqlSettingsRepository` (una `AsyncSession`, no comete `commit()`) y
`RequestScopedSettingsRepository` (una sesion por llamada, mismo criterio
que `panel.infrastructure.sql_read_model.RequestScopedPanelReadPort`) viven
separadas para que la primera se pueda probar contra `db_session` (fixture
de transaccion que siempre se deshace) sin arrastrar la apertura de sesion
propia -- mismo patron que `SqlPanelReadPort`/`RequestScopedPanelReadPort`."""

from __future__ import annotations

import uuid

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.settings.application.dto import SettingsView
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme

__all__ = ["RequestScopedSettingsRepository", "SqlSettingsRepository"]

_SELECT = text("""
    SELECT b.timezone, b.reference_currency AS currency, b.active_hours_start,
           b.active_hours_end, b.digest_hour, COALESCE(op.theme, 'system') AS theme
      FROM businesses b
      LEFT JOIN owner_preferences op ON op.owner_id = :owner_id
     WHERE b.id = :business_id
""")

_UPDATE_BUSINESS = text("""
    UPDATE businesses
       SET active_hours_start = :active_hours_start,
           active_hours_end   = :active_hours_end,
           digest_hour        = :digest_hour
     WHERE id = :business_id
    RETURNING timezone, reference_currency AS currency, active_hours_start, active_hours_end,
              digest_hour
""")

_UPSERT_THEME = text("""
    INSERT INTO owner_preferences (owner_id, theme, updated_at)
    VALUES (:owner_id, :theme, now())
    ON CONFLICT (owner_id) DO UPDATE SET theme = EXCLUDED.theme, updated_at = EXCLUDED.updated_at
""")


class SqlSettingsRepository:
    """Implementa `settings.application.ports.SettingsRepository` sobre UNA
    sesion (el limite de la transaccion lo pone quien la abre -- nunca hace
    `commit()` por su cuenta, mismo criterio que `SqlProposalRepository`)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        default_active_hours: ActiveHours,
        default_digest_hour: DigestHour,
    ) -> None:
        self._session = session
        self._default_active_hours = default_active_hours
        self._default_digest_hour = default_digest_hour

    async def get(self, business_id: str, owner_id: uuid.UUID) -> SettingsView | None:
        row = (
            await self._session.execute(
                _SELECT, {"business_id": business_id, "owner_id": owner_id}
            )
        ).mappings().one_or_none()
        return None if row is None else self._to_view(business_id, row)

    async def save(
        self,
        business_id: str,
        owner_id: uuid.UUID,
        *,
        active_hours: ActiveHours,
        digest_hour: DigestHour,
        theme: Theme,
    ) -> SettingsView | None:
        row = (
            await self._session.execute(
                _UPDATE_BUSINESS,
                {
                    "business_id": business_id,
                    "active_hours_start": active_hours.start,
                    "active_hours_end": active_hours.end,
                    "digest_hour": digest_hour.hour,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        await self._session.execute(_UPSERT_THEME, {"owner_id": owner_id, "theme": theme.value})
        return SettingsView(
            business_id=business_id,
            timezone=str(row["timezone"]),
            currency=str(row["currency"]),
            active_hours=active_hours,
            digest_hour=digest_hour,
            theme=theme,
        )

    def _to_view(self, business_id: str, row: RowMapping) -> SettingsView:
        return SettingsView(
            business_id=business_id,
            timezone=str(row["timezone"]),
            currency=str(row["currency"]),
            active_hours=self._resolved_active_hours(row),
            digest_hour=self._resolved_digest_hour(row),
            theme=Theme(row["theme"]),
        )

    def _resolved_active_hours(self, row: RowMapping) -> ActiveHours:
        start, end = row["active_hours_start"], row["active_hours_end"]
        if start is None or end is None:
            return self._default_active_hours
        return ActiveHours(start=start, end=end)

    def _resolved_digest_hour(self, row: RowMapping) -> DigestHour:
        hour = row["digest_hour"]
        return self._default_digest_hour if hour is None else DigestHour(hour=hour)


class RequestScopedSettingsRepository:
    """`SettingsRepository` real, una sesion por llamada
    (`container.session_factory()`): `composition/app.py` construye un
    unico `GetSettings`/`UpdateSettings` al arrancar, asi que esta
    envoltura es la que hace que cada `GET`/`PUT /settings` tenga su
    propia transaccion (mismo criterio que `RequestScopedPanelReadPort`)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_active_hours: ActiveHours,
        default_digest_hour: DigestHour,
    ) -> None:
        self._session_factory = session_factory
        self._default_active_hours = default_active_hours
        self._default_digest_hour = default_digest_hour

    async def get(self, business_id: str, owner_id: uuid.UUID) -> SettingsView | None:
        async with self._session_factory() as session:
            return await self._repository(session).get(business_id, owner_id)

    async def save(
        self,
        business_id: str,
        owner_id: uuid.UUID,
        *,
        active_hours: ActiveHours,
        digest_hour: DigestHour,
        theme: Theme,
    ) -> SettingsView | None:
        async with self._session_factory() as session:
            view = await self._repository(session).save(
                business_id,
                owner_id,
                active_hours=active_hours,
                digest_hour=digest_hour,
                theme=theme,
            )
            if view is not None:
                await session.commit()
        return view

    def _repository(self, session: AsyncSession) -> SqlSettingsRepository:
        return SqlSettingsRepository(
            session,
            default_active_hours=self._default_active_hours,
            default_digest_hour=self._default_digest_hour,
        )
