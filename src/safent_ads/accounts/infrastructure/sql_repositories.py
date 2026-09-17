"""Repositorios SQL de `accounts` sobre `platform_accounts` y `ad_entities`
(migraciones 0001/0003/0011).

Mapeo imperativo con SQL de mano y parametros ligados: ninguna clase de
SQLAlchemy toca el dominio (data-model.md: "sin ORM, sin decoradores"). Lo
que entra y sale de aqui son agregados, no filas.

Los repositorios **no confirman**: hacen `flush` para que la propia unidad
de trabajo vea lo escrito, y la transaccion la cierra `application`
(plan.md §8).

Convenciones del mapeo, todas por escrito porque son decisiones, no obviedad:

- Enums: el dominio habla en minusculas (`active`), el esquema en mayusculas
  (`ACTIVE`, y el indice parcial `WHERE status = 'DRIFTED'`). La traduccion
  vive aqui y en ningun otro sitio.
- Dinero: `Money` son unidades minimas enteras y asi se guardan
  (`*_amount_minor`, migracion 0011).
- Jerarquia: el dominio referencia al padre por `EntityRef`; la tabla usa
  `parent_id` mas `platform_account_id`. Una campana tiene `parent_ref` de
  nivel `account`: ahi `parent_id` es NULL y la cuenta se resuelve por
  `(platform, external_account_id)`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.errors import AdEntityParentNotFoundError
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.errors import AccountOwnershipConflictError
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_account import (
    ApiTier,
    PlatformAccount,
    PlatformAccountStatus,
)
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

__all__ = ["SqlAccountRepository", "SqlAdEntityRepository"]

_SELECT_ACCOUNT: Final = """
    SELECT business_id, connection_id, platform, external_account_id, currency, timezone, api_tier,
           credential_ref_id, status, last_synced_at
      FROM platform_accounts
"""

_UPSERT_ACCOUNT: Final = """
    INSERT INTO platform_accounts (business_id, platform, external_account_id, currency,
                                   timezone, api_tier, credential_ref_id, status,
                                   last_synced_at, connection_id)
    VALUES (:business_id, :platform, :external_account_id, :currency, :timezone, :api_tier,
            :credential_ref_id, :status, :last_synced_at, :connection_id)
    ON CONFLICT (platform, connection_id, external_account_id) DO UPDATE
        SET currency          = EXCLUDED.currency,
            timezone          = EXCLUDED.timezone,
            api_tier          = EXCLUDED.api_tier,
            credential_ref_id = EXCLUDED.credential_ref_id,
            status            = EXCLUDED.status,
            last_synced_at    = EXCLUDED.last_synced_at
        WHERE platform_accounts.business_id = EXCLUDED.business_id
    RETURNING id
"""

_SELECT_ENTITY: Final = """
    SELECT entity.business_id,
           entity.entity_ref,
           entity.name,
           entity.status,
           entity.platform_state_hash,
           entity.is_controllable,
           entity.learning_state,
           entity.budget_amount_minor,
           entity.budget_currency,
           entity.budget_kind,
           entity.shared_budget_ref,
           entity.bid_target_amount_minor,
           entity.bid_target_currency,
           COALESCE(
               parent.entity_ref,
               account.platform || ':account:' ||
               CASE WHEN account.connection_id IS NULL THEN '' ELSE
                  account.business_id::text || ':' || account.connection_id::text || ':' END ||
               account.external_account_id
           ) AS parent_ref
      FROM ad_entities AS entity
      JOIN platform_accounts AS account ON account.id = entity.platform_account_id
      LEFT JOIN ad_entities AS parent ON parent.id = entity.parent_id
"""

# El padre se resuelve en la propia sentencia: o es una entidad conocida por
# su `entity_ref`, o es la cuenta (`parent_external_id` solo viene informado
# cuando el padre es de nivel `account`). Si no resuelve, la insercion no
# afecta a ninguna fila y el repositorio lo convierte en error explicito en
# vez de dejar una entidad huerfana.
_UPSERT_ENTITY: Final = """
    INSERT INTO ad_entities (business_id, platform_account_id, platform, level, external_id,
                             parent_id, name, status, budget_amount_minor, budget_currency,
                             budget_kind, shared_budget_ref, bid_target_amount_minor,
                             bid_target_currency, learning_state, platform_state_hash,
                             is_controllable, connection_id)
    SELECT :business_id, resolved.platform_account_id, :platform, :level, :external_id,
           resolved.parent_id, :name, :status, :budget_amount_minor, :budget_currency,
           :budget_kind, :shared_budget_ref, :bid_target_amount_minor, :bid_target_currency,
           :learning_state, :platform_state_hash, :is_controllable, :connection_id
      FROM (
            SELECT candidate.id AS parent_id, candidate.platform_account_id
              FROM ad_entities AS candidate
             WHERE candidate.entity_ref = :parent_ref
               AND candidate.business_id = :business_id
               AND candidate.connection_id IS NOT DISTINCT FROM :connection_id
            UNION ALL
            SELECT NULL::uuid AS parent_id, account.id AS platform_account_id
              FROM platform_accounts AS account
             WHERE account.platform = :platform
               AND account.external_account_id = :parent_external_id
               AND account.business_id = :business_id
               AND account.connection_id IS NOT DISTINCT FROM :connection_id
           ) AS resolved
    ON CONFLICT (platform, connection_id, level, external_id) DO UPDATE
        SET name                    = EXCLUDED.name,
            status                  = EXCLUDED.status,
            budget_amount_minor     = EXCLUDED.budget_amount_minor,
            budget_currency         = EXCLUDED.budget_currency,
            budget_kind             = EXCLUDED.budget_kind,
            shared_budget_ref       = EXCLUDED.shared_budget_ref,
            bid_target_amount_minor = EXCLUDED.bid_target_amount_minor,
            bid_target_currency     = EXCLUDED.bid_target_currency,
            learning_state          = EXCLUDED.learning_state,
            platform_state_hash     = EXCLUDED.platform_state_hash,
            is_controllable         = EXCLUDED.is_controllable
    RETURNING id
"""

# Padres antes que hijos: la FK y el trigger de coherencia de 0003 rechazan
# una entidad cuyo padre aun no existe.
_LEVEL_ORDER: Final[dict[EntityLevel, int]] = {
    EntityLevel.ACCOUNT: 0,
    EntityLevel.CAMPAIGN: 1,
    EntityLevel.AD_SET: 2,
    EntityLevel.AD: 3,
    EntityLevel.CREATIVE: 4,
}


class SqlAccountRepository:
    """`AccountRepository` (application/ports.py) sobre `platform_accounts`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_ref(self, account_ref: AccountRef) -> PlatformAccount | None:
        result = await self._session.execute(
            text(
                f"{_SELECT_ACCOUNT} WHERE platform = :platform AND external_account_id = :id "
                "AND connection_id IS NOT DISTINCT FROM :connection_id"
            ),
            {
                "platform": account_ref.platform.value,
                "id": account_ref.external_account_id,
                "connection_id": account_ref.connection_id,
            },
        )
        row = result.mappings().one_or_none()
        if row is None or (
            account_ref.business_id is not None and row["business_id"] != account_ref.business_id
        ):
            return None
        return _to_account(row)

    async def list_by_business(self, business_id: BusinessId) -> Sequence[PlatformAccount]:
        result = await self._session.execute(
            text(
                f"{_SELECT_ACCOUNT} WHERE business_id = :business_id "
                "ORDER BY platform, external_account_id"
            ),
            {"business_id": business_id.value},
        )
        return [_to_account(row) for row in result.mappings()]

    async def list_for_observation(self, business_id: BusinessId) -> Sequence[PlatformAccount]:
        """One stable ACTIVE route per physical account; never credential fallback.

        Inventory remains connection-scoped. A denied/revoked credential must
        fail at the broker; this selector does not retry with another identity.
        Only an explicit account status change changes the selected route.
        """
        result = await self._session.execute(
            text(f"""{_SELECT_ACCOUNT}
                WHERE id IN (
                    SELECT DISTINCT ON (business_id,platform,external_account_id) id
                    FROM platform_accounts
                    WHERE business_id=:business_id AND status='ACTIVE'
                    ORDER BY business_id,platform,external_account_id,created_at,id
                ) ORDER BY platform,external_account_id"""),  # noqa: S608 - constant SQL
            {"business_id": business_id.value},
        )
        return [_to_account(row) for row in result.mappings()]

    async def find_first_active(
        self, business_id: BusinessId, platform: PlatformCode
    ) -> PlatformAccount | None:
        """The business's oldest still-ACTIVE connection for `platform`:
        the deterministic binding `search_competitor_ads` authenticates
        with when the tool has no `account_ref` argument of its own
        (`mcp/infrastructure/broker_competitor_research_port.py`, fix/
        ad-library-over-composio) -- `None`, never an error, when the
        business connected no ACTIVE account for it yet."""
        result = await self._session.execute(
            text(
                f"{_SELECT_ACCOUNT} WHERE business_id = :business_id AND platform = :platform "
                "AND status = 'ACTIVE' ORDER BY created_at LIMIT 1"
            ),
            {"business_id": business_id.value, "platform": platform.value},
        )
        row = result.mappings().first()
        return None if row is None else _to_account(row)

    async def save(self, account: PlatformAccount) -> None:
        if account.account_ref.business_id not in (None, account.business_id.value):
            raise AccountOwnershipConflictError("account_business_mismatch")
        if (
            account.connection_owner_id is not None
            and account.account_ref.connection_id is not None
        ):
            result = await self._session.execute(
                text("""
                INSERT INTO platform_connections(id,business_id,owner_id,platform)
                VALUES(:id,:business,:owner,:platform)
                ON CONFLICT(id) DO UPDATE SET id=EXCLUDED.id
                WHERE platform_connections.business_id=EXCLUDED.business_id
                  AND platform_connections.owner_id=EXCLUDED.owner_id
                  AND platform_connections.platform=EXCLUDED.platform
                RETURNING id
            """),
                {
                    "id": account.account_ref.connection_id,
                    "business": account.business_id.value,
                    "owner": account.connection_owner_id,
                    "platform": account.account_ref.platform.value,
                },
            )
            if result.first() is None:
                raise AccountOwnershipConflictError("connection_owner_mismatch")
        result = await self._session.execute(text(_UPSERT_ACCOUNT), _account_params(account))
        if result.first() is None:
            raise AccountOwnershipConflictError("account_business_mismatch")
        await self._session.flush()

    async def exists_for_platform(self, platform: PlatformCode) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM platform_accounts WHERE platform = :platform LIMIT 1"),
            {"platform": platform.value},
        )
        return result.first() is not None


class SqlAdEntityRepository:
    """`AdEntityRepository` (application/ports.py) sobre `ad_entities`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_ref(self, entity_ref: EntityRef) -> AdEntity | None:
        result = await self._session.execute(
            text(f"{_SELECT_ENTITY} WHERE entity.entity_ref = :entity_ref"),
            {"entity_ref": str(entity_ref)},
        )
        row = result.mappings().one_or_none()
        return None if row is None else _to_entity(row)

    async def list_by_account(self, account_ref: AccountRef) -> Sequence[AdEntity]:
        result = await self._session.execute(
            text(
                f"{_SELECT_ENTITY} WHERE account.platform = :platform "
                "AND account.external_account_id = :external_account_id "
                "AND account.connection_id IS NOT DISTINCT FROM :connection_id "
                "ORDER BY entity.level, entity.external_id"
            ),
            {
                "platform": account_ref.platform.value,
                "external_account_id": account_ref.external_account_id,
                "connection_id": account_ref.connection_id,
            },
        )
        return [_to_entity(row) for row in result.mappings()]

    async def save(self, entity: AdEntity) -> None:
        if entity.entity_ref.business_id not in (None, entity.business_id.value):
            raise AccountOwnershipConflictError("entity_business_mismatch")
        result = await self._session.execute(text(_UPSERT_ENTITY), _entity_params(entity))
        if result.first() is None:
            raise AdEntityParentNotFoundError(
                f"{entity.entity_ref} declara el padre {entity.parent_ref}, que no existe"
            )
        await self._session.flush()

    async def save_many(self, entities: Sequence[AdEntity]) -> None:
        for entity in sorted(entities, key=lambda item: _LEVEL_ORDER[item.entity_ref.level]):
            await self.save(entity)


def _account_params(account: PlatformAccount) -> Mapping[str, Any]:
    return {
        "connection_id": account.account_ref.connection_id,
        "business_id": account.business_id.value,
        "platform": account.account_ref.platform.value,
        "external_account_id": account.account_ref.external_account_id,
        "currency": account.currency,
        "timezone": account.timezone,
        "api_tier": account.api_tier.value,
        "credential_ref_id": account.credential_ref_id.value,
        "status": account.status.value.upper(),
        "last_synced_at": account.last_synced_at,
    }


def _entity_params(entity: AdEntity) -> Mapping[str, Any]:
    budget_amount, budget_currency, budget_kind = _budget_columns(entity.budget)
    bid_amount, bid_currency = _money_columns(entity.bid_target)
    parent_is_account = entity.parent_ref.level == EntityLevel.ACCOUNT
    return {
        "connection_id": entity.entity_ref.connection_id,
        "business_id": entity.business_id.value,
        "platform": entity.entity_ref.platform.value,
        "level": entity.entity_ref.level.value,
        "external_id": entity.entity_ref.external_id,
        "parent_ref": str(entity.parent_ref),
        "parent_external_id": entity.parent_ref.external_id if parent_is_account else None,
        "name": entity.name,
        "status": entity.status.value.upper(),
        "budget_amount_minor": budget_amount,
        "budget_currency": budget_currency,
        "budget_kind": budget_kind,
        "shared_budget_ref": _optional_ref(entity.shared_budget_ref),
        "bid_target_amount_minor": bid_amount,
        "bid_target_currency": bid_currency,
        "learning_state": entity.learning_state.value.upper(),
        "platform_state_hash": entity.platform_state_hash.value,
        "is_controllable": entity.is_controllable,
    }


def _budget_columns(budget: Budget | None) -> tuple[int | None, str | None, str | None]:
    if budget is None:
        return None, None, None
    return budget.amount.minor_units, budget.amount.currency, budget.kind.value


def _money_columns(money: Money | None) -> tuple[int | None, str | None]:
    if money is None:
        return None, None
    return money.minor_units, money.currency


def _optional_ref(ref: EntityRef | None) -> str | None:
    return None if ref is None else str(ref)


def _to_account(row: RowMapping) -> PlatformAccount:
    return PlatformAccount(
        business_id=BusinessId(row["business_id"]),
        account_ref=AccountRef(
            platform=PlatformCode(row["platform"]),
            external_account_id=row["external_account_id"],
            connection_id=row["connection_id"],
            business_id=row["business_id"] if row["connection_id"] else None,
        ),
        currency=row["currency"],
        timezone=row["timezone"],
        api_tier=ApiTier(row["api_tier"]),
        credential_ref_id=CredentialRefId(row["credential_ref_id"]),
        status=PlatformAccountStatus(row["status"].lower()),
        last_synced_at=row["last_synced_at"],
    )


def _to_entity(row: RowMapping) -> AdEntity:
    return AdEntity(
        business_id=BusinessId(row["business_id"]),
        entity_ref=EntityRef.parse(row["entity_ref"]),
        parent_ref=EntityRef.parse(row["parent_ref"]),
        name=row["name"],
        status=AdEntityStatus(row["status"].lower()),
        platform_state_hash=PlatformStateHash(row["platform_state_hash"]),
        is_controllable=row["is_controllable"],
        learning_state=LearningState(row["learning_state"].lower()),
        budget=_to_budget(row),
        bid_target=_to_money(row["bid_target_amount_minor"], row["bid_target_currency"]),
        shared_budget_ref=(
            None if row["shared_budget_ref"] is None else EntityRef.parse(row["shared_budget_ref"])
        ),
    )


def _to_budget(row: RowMapping) -> Budget | None:
    amount = _to_money(row["budget_amount_minor"], row["budget_currency"])
    if amount is None:
        return None
    return Budget(amount=amount, kind=BudgetKind(row["budget_kind"]))


def _to_money(minor_units: int | None, currency: str | None) -> Money | None:
    if minor_units is None or currency is None:
        return None
    return Money(minor_units, currency)
