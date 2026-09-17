"""Motor async y fabrica de sesiones de SQLAlchemy: **uno por proceso**.

`ads-api`, `ads-worker` y `ads-broker` son procesos distintos (plan.md §3):
cada uno construye su propio `Database` desde sus settings y comparte ese
unico pool con todo lo que vive dentro. Crear un motor por peticion o por
caso de uso agota conexiones y rompe `pool_pre_ping`.

Aqui no hay logica de negocio ni SQL: solo el cableado.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from safent_ads.composition.settings import CommonSettings

__all__ = ["Database", "create_engine", "create_session_factory"]


def create_engine(database_url: str) -> AsyncEngine:
    """`pool_pre_ping`: una conexion que el servidor cerro por su cuenta no
    se entrega a un caso de uso, se reconecta."""
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """`expire_on_commit=False`: tras confirmar, los objetos leidos siguen
    siendo utilizables sin volver a la base."""
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(frozen=True, slots=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def from_settings(cls, settings: CommonSettings) -> Database:
        engine = create_engine(settings.database_url.get_secret_value())
        return cls(engine=engine, session_factory=create_session_factory(engine))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unidad de trabajo de un caso de uso: confirma al salir sin error,
        deshace ante cualquier excepcion (plan.md §8). Los repositorios no
        confirman; la frontera transaccional es de `application`."""
        async with self.session_factory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            await session.commit()

    async def aclose(self) -> None:
        await self.engine.dispose()
