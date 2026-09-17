"""Comprobacion IDOR compartida por los puertos de lectura que reciben un
`account_ref` en bruto (`broker_gaql_read_port.py`,
`broker_search_term_read_port.py`): parsea la cadena y comprueba que la
cuenta pertenece de verdad al `business_id` que llama contra
`platform_accounts` (`SqlAccountRepository.get_by_ref`). Cuenta inexistente
o de otro negocio devuelven el mismo `EntityNotFoundError` (IDOR-safe: no
se filtra cual de los dos casos es) -- unico lugar donde vive esta regla
para que no diverja entre puertos que hoy la comparten letra a letra."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.domain.refs import AccountRef, AccountRefFormatError
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.mcp.application.errors import EntityNotFoundError

__all__ = ["resolve_owned_account_ref"]


async def resolve_owned_account_ref(
    session_factory: async_sessionmaker[AsyncSession], business_id: str, account_ref: str
) -> AccountRef:
    ref = _parse_account_ref(account_ref)
    async with session_factory() as session:
        account = await SqlAccountRepository(session).get_by_ref(ref)
    if account is None or str(account.business_id) != business_id:
        raise EntityNotFoundError(f"{account_ref} aun no disponible")
    return ref


def _parse_account_ref(account_ref: str) -> AccountRef:
    try:
        return AccountRef.parse(account_ref)
    except AccountRefFormatError as exc:
        raise EntityNotFoundError(f"{account_ref} aun no disponible") from exc
