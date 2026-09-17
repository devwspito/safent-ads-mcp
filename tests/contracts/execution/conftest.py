"""Banco de pruebas de contrato de los puertos de `execution`.

Los mismos casos corren dos veces: contra los dobles en memoria
(`execution/testing/fakes.py`, rapido, sin docker) y contra los adaptadores
SQL sobre Postgres real (marcados `integration`). Si una implementacion se
desvia de la otra, el caso falla en una de las dos ejecuciones: eso es lo que
significa "sustituible" (LSP).

Los prerrequisitos de integridad referencial (negocio -> cuenta -> entidad ->
propuesta -> autorizacion) los prepara `given_authorized_proposal`: en
memoria no hay nada que preparar; en SQL, saltarselos es chocar con las FK
compuestas que impiden que una ejecucion apunte al negocio de otro (C-27)."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import (
    EntityStateSnapshot,
    IdempotencyKey,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.execution.application.ports import (
    AdsPlatformWritePort,
    BrakeStatePort,
    ExecutionQueuePort,
    FreshnessPort,
    GuardrailSetRepository,
    PlatformReaderPort,
    RuleConditionPort,
)
from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    build_idempotency_key,
)
from safent_ads.execution.domain.guardrails import (
    BrakeScope,
    BrakeScopeKind,
    GuardrailScope,
    GuardrailSet,
    ScopeKind,
    SpendLedger,
    brake_scope_from,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.broker_platform import (
    BrokerPlatformReader,
    BrokerPlatformWriter,
)
from safent_ads.execution.infrastructure.errors import PlatformWriteDeniedError
from safent_ads.execution.infrastructure.sql_brake_state import SqlBrakeStatePort
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_freshness import SqlFreshnessPort
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.execution.infrastructure.sql_rule_condition import SqlRuleConditionPort
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.execution.testing.fakes import (
    FakeAdsPlatformWritePort,
    FakeBrakeStatePort,
    FakeExecutionQueuePort,
    FakeFreshnessPort,
    FakeGuardrailSetRepository,
    FakePlatformReaderPort,
    FakeRuleConditionPort,
    FakeSpendLedger,
)
from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.infrastructure.value_codec import money_from_minor, money_to_minor
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence as SignalEvidence
from safent_ads.signals.domain.value_objects import MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlCreativeSignalRepository,
    SqlSignalRepository,
)
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.accounts.application.conftest import FakeAdsPlatformPort

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def digest(seed: str) -> str:
    """Hash sha256 en hexadecimal: la forma que exigen `proposals.diff_hash`,
    `approvals.guardrail_verdict_hash` y `executions.platform_state_hash_*`."""
    return hashlib.sha256(seed.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizedProposal:
    """Lo minimo que necesita un `ExecutionAttempt` para existir: a que
    negocio, propuesta, autorizacion y entidad pertenece."""

    business_id: BusinessId
    proposal_id: ProposalId
    authorization_id: AuthorizationId
    entity_ref: EntityRef
    diff_hash: str
    expected_state_hash: str


class QueueFixture(Protocol):
    queue: ExecutionQueuePort

    async def given_authorized_proposal(self) -> AuthorizedProposal:
        """Propuesta programada con autorizacion viva, lista para ejecutar."""

    async def enqueue(self, attempt: ExecutionAttempt) -> None:
        """Deja el intento reclamable. En memoria es la cola FIFO del doble;
        en SQL, una fila `CLAIMED` sin reclamar (`started_at IS NULL`)."""


@dataclass(slots=True)
class InMemoryQueueFixture:
    queue: FakeExecutionQueuePort

    async def given_authorized_proposal(self) -> AuthorizedProposal:
        seed = uuid.uuid4().hex
        return AuthorizedProposal(
            business_id=BusinessId.new(),
            proposal_id=ProposalId.new(),
            authorization_id=AuthorizationId.new(),
            entity_ref=campaign_ref(f"c-{seed[:10]}"),
            diff_hash=digest(f"diff-{seed}"),
            expected_state_hash=digest(f"state-{seed}"),
        )

    async def enqueue(self, attempt: ExecutionAttempt) -> None:
        self.queue.enqueue(attempt)


@dataclass(slots=True)
class SqlQueueFixture:
    queue: SqlExecutionQueue
    session: AsyncSession

    async def given_authorized_proposal(self) -> AuthorizedProposal:
        return await seed_authorized_proposal(self.session)

    async def enqueue(self, attempt: ExecutionAttempt) -> None:
        await self.queue.save(attempt)


async def seed_authorized_proposal(session: AsyncSession) -> AuthorizedProposal:
    """Inserta negocio, credencial, cuenta, entidad, propuesta y aprobacion.
    Es lo que habra hecho el ciclo de propuestas antes de que el chokepoint
    reclame nada."""
    context = await seed_proposal(session)
    await _seed_approval(session, context)
    return context


async def seed_proposal(session: AsyncSession) -> AuthorizedProposal:
    """Solo hasta la propuesta: sin ninguna decision anexada todavia."""
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
    business_id = await seed_entity(session, entity_ref)
    proposal_id = uuid.uuid4()
    diff_hash = digest(f"diff-{proposal_id}")
    expected_state_hash = digest(f"state-{proposal_id}")
    await session.execute(
        text(
            """
            INSERT INTO proposals (id, business_id, entity_ref, parameter, current_value,
                                   proposed_value, diff_hash, classification, cause_key, cause,
                                   evidence, estimated_impact_amount, estimated_impact_currency,
                                   urgency, state, execution_scheduled_at, expires_at)
            VALUES (:id, :business_id, :entity_ref, 'daily_budget',
                    CAST(:current_value AS JSONB), CAST(:proposed_value AS JSONB), :diff_hash,
                    'routine', 'M05|budget_high', 'CPL sobre objetivo en 7D',
                    CAST(:evidence AS JSONB), 310, 'EUR', 'recommended', 'scheduled',
                    :scheduled_at, :expires_at)
            """
        ),
        {
            "id": proposal_id,
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "current_value": json.dumps({"type": "money", "amount": "100", "currency": "EUR"}),
            "proposed_value": json.dumps({"type": "money", "amount": "70", "currency": "EUR"}),
            "diff_hash": diff_hash,
            "evidence": json.dumps({"expected_state_hash": expected_state_hash}),
            "scheduled_at": NOW,
            "expires_at": NOW.replace(hour=23),
        },
    )
    await session.flush()
    return AuthorizedProposal(
        business_id=BusinessId(business_id),
        proposal_id=ProposalId(proposal_id),
        authorization_id=AuthorizationId(uuid.uuid4()),
        entity_ref=entity_ref,
        diff_hash=diff_hash,
        expected_state_hash=expected_state_hash,
    )


async def _seed_approval(session: AsyncSession, context: AuthorizedProposal) -> None:
    """La firma viaja en hexadecimal, como la escribe el adaptador real."""
    await session.execute(
        text(
            """
            INSERT INTO approvals (id, proposal_id, kind, decision, diff_hash,
                                   guardrail_verdict_hash, issued_by, channel, signature,
                                   decided_at, expires_at)
            VALUES (:id, :proposal_id, 'human_approval', 'approved', :diff_hash, :verdict_hash,
                    'owner-de-contrato', 'panel', :signature, :decided_at, :expires_at)
            """
        ),
        {
            "id": uuid.UUID(str(context.authorization_id)),
            "proposal_id": uuid.UUID(str(context.proposal_id)),
            "diff_hash": context.diff_hash,
            "verdict_hash": digest(f"verdict-{context.proposal_id}"),
            "signature": digest(f"firma-{context.proposal_id}"),
            "decided_at": NOW,
            "expires_at": NOW.replace(hour=23),
        },
    )
    await session.flush()


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def queue(request: pytest.FixtureRequest) -> AsyncIterator[QueueFixture]:
    if request.param == "in_memory":
        yield InMemoryQueueFixture(queue=FakeExecutionQueuePort())
        return
    # `isolated_database_url` es una fixture sincrona: pedirla aqui solo
    # levanta el contenedor cuando corre la variante SQL, nunca para la de
    # memoria. Base aparte porque el reclamo de la cola no filtra por negocio.
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlQueueFixture(queue=SqlExecutionQueue(session, FixedClock(NOW)), session=session)


@dataclass(frozen=True, slots=True)
class AccountWithBusiness:
    """Una cuenta y el negocio que la posee -- lo que `get_effective`
    necesita para probar que un freno de negocio alcanza a sus cuentas."""

    account_scope: BrakeScope
    business_scope: BrakeScope


class BrakeFixture(Protocol):
    brakes: BrakeStatePort

    async def given_account_scope(self) -> BrakeScope:
        """Ambito de cuenta tal como lo compone `brake_scope_from` desde la
        propuesta: el `ref` es la entidad, y el adaptador real resuelve su
        cuenta. En memoria basta con que la clave sea estable."""

    def given_global_scope(self) -> BrakeScope:
        """Freno global (FR-14): no cuelga de ninguna cuenta."""

    async def given_account_with_business(self) -> AccountWithBusiness:
        """Una cuenta nueva y el ambito de NEGOCIO que la posee de verdad
        (`get_effective` resuelve esa propiedad; en memoria no hay tabla que
        consultar, asi que el doble registra la relacion explicitamente)."""


@dataclass(slots=True)
class InMemoryBrakeFixture:
    brakes: FakeBrakeStatePort

    async def given_account_scope(self) -> BrakeScope:
        return brake_scope_from(
            GuardrailScope(kind=ScopeKind.ENTITY, ref=str(campaign_ref(uuid.uuid4().hex[:10])))
        )

    def given_global_scope(self) -> BrakeScope:
        return BrakeScope(kind=BrakeScopeKind.GLOBAL)

    async def given_account_with_business(self) -> AccountWithBusiness:
        account_scope = await self.given_account_scope()
        business_ref = str(uuid.uuid4())
        self.brakes.link_account_to_business(account_scope.ref or "", business_ref)
        return AccountWithBusiness(
            account_scope=account_scope,
            business_scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref=business_ref),
        )


@dataclass(slots=True)
class SqlBrakeFixture:
    brakes: SqlBrakeStatePort
    session: AsyncSession

    async def given_account_scope(self) -> BrakeScope:
        account_scope, _business_scope = await self._seed_account_and_business()
        return account_scope

    def given_global_scope(self) -> BrakeScope:
        return BrakeScope(kind=BrakeScopeKind.GLOBAL)

    async def given_account_with_business(self) -> AccountWithBusiness:
        account_scope, business_scope = await self._seed_account_and_business()
        return AccountWithBusiness(account_scope=account_scope, business_scope=business_scope)

    async def _seed_account_and_business(self) -> tuple[BrakeScope, BrakeScope]:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        business_id = await seed_entity(self.session, entity_ref)
        account_scope = brake_scope_from(GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)))
        business_scope = BrakeScope(kind=BrakeScopeKind.BUSINESS, ref=str(business_id))
        return account_scope, business_scope


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def brakes(request: pytest.FixtureRequest) -> AsyncIterator[BrakeFixture]:
    if request.param == "in_memory":
        yield InMemoryBrakeFixture(brakes=FakeBrakeStatePort())
        return
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlBrakeFixture(brakes=SqlBrakeStatePort(session), session=session)


class LedgerFixture(Protocol):
    ledger: SpendLedger

    async def given_entity_with_a_running_execution(self) -> EntityRef:
        """Entidad con un intento ya reclamado: el ledger atribuye a el todo
        cambio que se aplique. En memoria no hay nada que preparar."""

    async def recorded_deltas(self, entity_ref: EntityRef) -> list[Money]:
        """Lo que el ledger guardo del cambio, visto desde fuera."""


@dataclass(slots=True)
class InMemoryLedgerFixture:
    ledger: FakeSpendLedger

    async def given_entity_with_a_running_execution(self) -> EntityRef:
        return campaign_ref(f"c-{uuid.uuid4().hex[:10]}")

    async def recorded_deltas(self, entity_ref: EntityRef) -> list[Money]:
        return [
            delta for _scope, ref, delta in self.ledger.recorded_changes if ref == entity_ref
        ]


@dataclass(slots=True)
class SqlLedgerFixture:
    ledger: SqlSpendLedger
    queue: SqlExecutionQueue
    session: AsyncSession

    async def given_entity_with_a_running_execution(self) -> EntityRef:
        context = await seed_authorized_proposal(self.session)
        attempt = ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=context.business_id,
            proposal_id=context.proposal_id,
            authorization_id=context.authorization_id,
            idempotency_key=build_idempotency_key(context.proposal_id, context.diff_hash),
            previous_value=Money.of("100"),
            platform_state_hash_before=context.expected_state_hash,
            attempt_count=0,
        )
        await self.queue.save(attempt)
        assert await self.queue.claim_next() is not None
        return context.entity_ref

    async def recorded_deltas(self, entity_ref: EntityRef) -> list[Money]:
        result = await self.session.execute(
            text(
                """
                SELECT delta_minor, currency FROM spend_ledger
                 WHERE entity_ref = :entity_ref AND kind = 'applied_change'
                 ORDER BY id
                """
            ),
            {"entity_ref": str(entity_ref)},
        )
        return [money_from_minor(int(row.delta_minor), row.currency) for row in result.all()]


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def ledger(request: pytest.FixtureRequest) -> AsyncIterator[LedgerFixture]:
    if request.param == "in_memory":
        yield InMemoryLedgerFixture(ledger=FakeSpendLedger({}))
        return
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        queue = SqlExecutionQueue(session, FixedClock(NOW))
        yield SqlLedgerFixture(
            ledger=SqlSpendLedger(session, FixedClock(NOW), queue), queue=queue, session=session
        )


@dataclass(frozen=True, slots=True)
class GuardrailLimits:
    """Los limites de un ambito, en las unidades del dominio."""

    daily_cap: str = "500"
    monthly_cap: str = "10000"
    floor: str = "10"
    ceiling: str = "300"
    max_step_pct: float = 0.30
    max_changes_per_entity_day: int = 2

    def as_set(self, scope: GuardrailScope) -> GuardrailSet:
        return GuardrailSet(
            scope=scope,
            daily_cap=Money.of(self.daily_cap),
            monthly_cap=Money.of(self.monthly_cap),
            floor=Money.of(self.floor),
            ceiling=Money.of(self.ceiling),
            max_step_pct=self.max_step_pct,
            max_changes_per_entity_day=self.max_changes_per_entity_day,
        )


class GuardrailFixture(Protocol):
    guardrails: GuardrailSetRepository

    async def given_entity_without_guardrails(self) -> GuardrailScope:
        """Entidad real sin ningun guardarrail que la alcance."""

    async def given_business_guardrails(self, limits: GuardrailLimits) -> GuardrailScope:
        """Entidad cuyo negocio tiene guardarrailes; ningun ambito mas
        especifico los estrecha."""


@dataclass(slots=True)
class InMemoryGuardrailFixture:
    guardrails: FakeGuardrailSetRepository
    configured: dict[str, GuardrailSet]

    async def given_entity_without_guardrails(self) -> GuardrailScope:
        return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(campaign_ref(uuid.uuid4().hex[:10])))

    async def given_business_guardrails(self, limits: GuardrailLimits) -> GuardrailScope:
        scope = await self.given_entity_without_guardrails()
        self.configured[scope.ref] = limits.as_set(scope)
        return scope


@dataclass(slots=True)
class SqlGuardrailFixture:
    guardrails: SqlGuardrailSetRepository
    session: AsyncSession

    async def given_entity_without_guardrails(self) -> GuardrailScope:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        await seed_entity(self.session, entity_ref)
        return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))

    async def given_business_guardrails(self, limits: GuardrailLimits) -> GuardrailScope:
        scope = await self.given_entity_without_guardrails()
        await seed_guardrails(self.session, scope=scope, limits=limits, level="business")
        return scope


async def seed_guardrails(
    session: AsyncSession,
    *,
    scope: GuardrailScope,
    limits: GuardrailLimits,
    level: str,
) -> None:
    """Fila de `guardrails` en el ambito pedido para la entidad de `scope`.
    `campaign` cuelga de la entidad; `business`/`platform_account`, de sus
    duenos."""
    await session.execute(
        text(_INSERT_GUARDRAILS[level]),
        {
            "entity_ref": scope.ref,
            "daily_cap": _minor(limits.daily_cap),
            "monthly_cap": _minor(limits.monthly_cap),
            "floor": _minor(limits.floor),
            "ceiling": _minor(limits.ceiling),
            "max_step_pct": limits.max_step_pct * 100,
            "max_changes": limits.max_changes_per_entity_day,
        },
    )
    await session.flush()


def _minor(amount: str) -> int:
    return money_to_minor(Money.of(amount))


# Una sentencia entera por ambito, sin componer SQL: asi se lee lo que se
# inserta y ruff no tiene que adivinar si hay interpolacion peligrosa.
_INSERT_GUARDRAILS = {
    "business": """
        INSERT INTO guardrails (scope, business_id, currency, daily_cap_minor,
                                monthly_cap_minor, budget_floor_minor, budget_ceiling_minor,
                                max_step_pct, max_changes_per_entity_per_day)
        SELECT 'business', entity.business_id, 'EUR', :daily_cap, :monthly_cap, :floor,
               :ceiling, :max_step_pct, :max_changes
          FROM ad_entities AS entity WHERE entity.entity_ref = :entity_ref
    """,
    "platform_account": """
        INSERT INTO guardrails (scope, platform_account_id, currency, daily_cap_minor,
                                monthly_cap_minor, budget_floor_minor, budget_ceiling_minor,
                                max_step_pct, max_changes_per_entity_per_day)
        SELECT 'platform_account', entity.platform_account_id, 'EUR', :daily_cap,
               :monthly_cap, :floor, :ceiling, :max_step_pct, :max_changes
          FROM ad_entities AS entity WHERE entity.entity_ref = :entity_ref
    """,
    "campaign": """
        INSERT INTO guardrails (scope, campaign_entity_ref, currency, daily_cap_minor,
                                monthly_cap_minor, budget_floor_minor, budget_ceiling_minor,
                                max_step_pct, max_changes_per_entity_per_day)
        SELECT 'campaign', entity.entity_ref, 'EUR', :daily_cap, :monthly_cap, :floor,
               :ceiling, :max_step_pct, :max_changes
          FROM ad_entities AS entity WHERE entity.entity_ref = :entity_ref
    """,
}


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def guardrails(request: pytest.FixtureRequest) -> AsyncIterator[GuardrailFixture]:
    if request.param == "in_memory":
        configured: dict[str, GuardrailSet] = {}
        yield InMemoryGuardrailFixture(
            guardrails=FakeGuardrailSetRepository(configured), configured=configured
        )
        return
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlGuardrailFixture(
            guardrails=SqlGuardrailSetRepository(session), session=session
        )


_INSERT_FRESHNESS = """
    INSERT INTO data_freshness (platform_account_id, entity_level, granularity,
                                last_ingested_at, lag_minutes, stale_threshold_minutes)
    SELECT entity.platform_account_id, entity.level, 'daily', :last_ingested_at, :lag_minutes, 60
      FROM ad_entities AS entity WHERE entity.entity_ref = :entity_ref
"""

# El catalogo nace deshabilitado y en NOTIFY (0007). Encender una regla es
# completarla: el esquema no admite habilitada sin accion, ventana, cooldown
# y tope de disparos.
_CALIBRATE_RULE = """
    UPDATE rules
       SET is_enabled = :enabled, autonomy_level = :autonomy_level, action = 'SELL',
           entity_level = 'campaign', data_window = '7D', cooldown_minutes = 60,
           max_firings_per_day = 3, magnitude_pct = 30,
           condition = CAST(:condition AS jsonb)
     WHERE code = :code AND scope = 'global'
"""

_M05_CONDITION = json.dumps(
    {
        "clauses": [
            {
                "metric": "roas",
                "comparator": "lt",
                "window": "7D",
                "threshold_kind": "absolute",
                "value": 2.0,
            }
        ]
    }
)
FIRING_RULE_CODE = "M05"


async def seed_freshness(
    session: AsyncSession, entity_ref: EntityRef, *, lag_minutes: int
) -> None:
    await session.execute(
        text(_INSERT_FRESHNESS),
        {
            "entity_ref": str(entity_ref),
            "lag_minutes": lag_minutes,
            "last_ingested_at": NOW - timedelta(minutes=lag_minutes),
        },
    )
    await session.flush()


async def calibrate_rule(
    session: AsyncSession,
    *,
    enabled: bool,
    code: str = FIRING_RULE_CODE,
    autonomy_level: str = "AUTO",
) -> None:
    """Completa la regla del catalogo (`sync_catalog` hace esto desde
    `rules.yaml`) y deja el interruptor donde pida el caso: encenderlo es
    decision del propietario, no del cargador (D-A1)."""
    await session.execute(
        text(_CALIBRATE_RULE),
        {
            "code": code,
            "condition": _M05_CONDITION,
            "enabled": enabled,
            "autonomy_level": autonomy_level,
        },
    )
    await session.flush()


async def seed_sell_signal(session: AsyncSession, entity_ref: EntityRef) -> str:
    """Senal viva que la regla M05 reconoce como suya (mismo codigo y mismo
    tipo). La produce el ciclo de senales; aqui se escribe con su propio
    repositorio, no a mano. Devuelve el `id` real que Postgres le asigno
    (`signals.id`), lo que `LiveRuleStep` debe enlazar en `proposals.
    signal_id` -- los llamadores que no lo necesitan simplemente lo
    ignoran."""
    repository = SqlSignalRepository(session, cycle_id=uuid.uuid4())
    await repository.save(
        Signal(
            entity_ref=entity_ref,
            kind=SignalKind.SELL,
            strength=SignalStrength(70),
            cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
            cause_sentence="ROAS por debajo del objetivo en 7D",
            span=WindowSpan.D7,
            money_at_stake=MoneyAtStake(minor_units=12_000, currency="EUR"),
            evidence=SignalEvidence(
                metric="roas", actual=1.4, target=2.0, baseline=None, span=WindowSpan.D7
            ),
            gate_verdicts=(),
            emitted_at=NOW,
            rule_code=FIRING_RULE_CODE,
        )
    )
    await session.flush()
    saved = await repository.find_latest_for_entity(entity_ref=entity_ref)
    assert saved is not None and saved.signal_id is not None  # noqa: S101 - fixture, no produccion
    return saved.signal_id


class FreshnessFixture(Protocol):
    freshness: FreshnessPort

    async def given_fresh_entity(self) -> EntityRef:
        """Entidad cuya cuenta ingesto hace un momento."""

    async def given_stale_entity(self) -> EntityRef:
        """Entidad cuya ingesta se quedo atras."""

    async def given_never_ingested_entity(self) -> EntityRef:
        """Entidad de la que nunca se ingesto nada."""


@dataclass(slots=True)
class InMemoryFreshnessFixture:
    freshness: FakeFreshnessPort
    stale_by_ref: dict[str, bool]

    async def given_fresh_entity(self) -> EntityRef:
        return self._register(stale=False)

    async def given_stale_entity(self) -> EntityRef:
        return self._register(stale=True)

    async def given_never_ingested_entity(self) -> EntityRef:
        return self._register(stale=True)

    def _register(self, *, stale: bool) -> EntityRef:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        self.stale_by_ref[str(entity_ref)] = stale
        return entity_ref


@dataclass(slots=True)
class SqlFreshnessFixture:
    freshness: SqlFreshnessPort
    session: AsyncSession

    async def given_fresh_entity(self) -> EntityRef:
        entity_ref = await self._entity()
        await seed_freshness(self.session, entity_ref, lag_minutes=5)
        return entity_ref

    async def given_stale_entity(self) -> EntityRef:
        entity_ref = await self._entity()
        await seed_freshness(self.session, entity_ref, lag_minutes=180)
        return entity_ref

    async def given_never_ingested_entity(self) -> EntityRef:
        return await self._entity()

    async def _entity(self) -> EntityRef:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        await seed_entity(self.session, entity_ref)
        return entity_ref


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def freshness(request: pytest.FixtureRequest) -> AsyncIterator[FreshnessFixture]:
    if request.param == "in_memory":
        stale_by_ref: dict[str, bool] = {}
        yield InMemoryFreshnessFixture(
            freshness=FakeFreshnessPort(lambda ref: stale_by_ref.get(str(ref), True)),
            stale_by_ref=stale_by_ref,
        )
        return
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlFreshnessFixture(
            freshness=SqlFreshnessPort(session, FixedClock(NOW)), session=session
        )


class RuleConditionFixture(Protocol):
    conditions: RuleConditionPort

    async def given_firing_rule(self) -> tuple[str, EntityRef]:
        """Regla encendida cuya senal viva la reconoce como suya."""

    async def given_quiet_rule(self) -> tuple[str, EntityRef]:
        """Regla encendida sin ninguna senal que dispare."""

    async def given_firing_rule_with_stale_data(self) -> tuple[str, EntityRef]:
        """La senal dispararia, pero los datos estan viejos."""

    async def given_disabled_rule(self) -> tuple[str, EntityRef]:
        """La senal dispararia, pero el propietario no encendio la regla."""

    async def given_firing_rule_with_approval_autonomy(self) -> tuple[str, EntityRef]:
        """La senal dispararia y la regla esta encendida, pero calibrada
        `APPROVAL`, no `AUTO`: no autoriza `rule_authorization` (FR-11/FR-12,
        contracts/mcp-tools.md comprobacion 1)."""


@dataclass(slots=True)
class InMemoryRuleConditionFixture:
    conditions: FakeRuleConditionPort
    live_by_key: dict[tuple[str, str], bool]

    async def given_firing_rule(self) -> tuple[str, EntityRef]:
        return self._register(live=True)

    async def given_quiet_rule(self) -> tuple[str, EntityRef]:
        return self._register(live=False)

    async def given_firing_rule_with_stale_data(self) -> tuple[str, EntityRef]:
        return self._register(live=False)

    async def given_disabled_rule(self) -> tuple[str, EntityRef]:
        return self._register(live=False)

    async def given_firing_rule_with_approval_autonomy(self) -> tuple[str, EntityRef]:
        # El doble en memoria no modela autonomia por regla (T067): la
        # variante SQL es la unica que puede probar este caso de verdad.
        return self._register(live=False)

    def _register(self, *, live: bool) -> tuple[str, EntityRef]:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        self.live_by_key[(FIRING_RULE_CODE, str(entity_ref))] = live
        return FIRING_RULE_CODE, entity_ref


@dataclass(slots=True)
class SqlRuleConditionFixture:
    conditions: SqlRuleConditionPort
    session: AsyncSession

    async def given_firing_rule(self) -> tuple[str, EntityRef]:
        entity_ref = await self._entity_with_signal(lag_minutes=5)
        await calibrate_rule(self.session, enabled=True)
        return FIRING_RULE_CODE, entity_ref

    async def given_quiet_rule(self) -> tuple[str, EntityRef]:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        await seed_entity(self.session, entity_ref)
        await seed_freshness(self.session, entity_ref, lag_minutes=5)
        await calibrate_rule(self.session, enabled=True)
        return FIRING_RULE_CODE, entity_ref

    async def given_firing_rule_with_stale_data(self) -> tuple[str, EntityRef]:
        entity_ref = await self._entity_with_signal(lag_minutes=180)
        await calibrate_rule(self.session, enabled=True)
        return FIRING_RULE_CODE, entity_ref

    async def given_disabled_rule(self) -> tuple[str, EntityRef]:
        entity_ref = await self._entity_with_signal(lag_minutes=5)
        await calibrate_rule(self.session, enabled=False)
        return FIRING_RULE_CODE, entity_ref

    async def given_firing_rule_with_approval_autonomy(self) -> tuple[str, EntityRef]:
        entity_ref = await self._entity_with_signal(lag_minutes=5)
        await calibrate_rule(self.session, enabled=True, autonomy_level="APPROVAL")
        return FIRING_RULE_CODE, entity_ref

    async def _entity_with_signal(self, *, lag_minutes: int) -> EntityRef:
        entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
        await seed_entity(self.session, entity_ref)
        await seed_freshness(self.session, entity_ref, lag_minutes=lag_minutes)
        await seed_sell_signal(self.session, entity_ref)
        return entity_ref


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def conditions(request: pytest.FixtureRequest) -> AsyncIterator[RuleConditionFixture]:
    if request.param == "in_memory":
        live_by_key: dict[tuple[str, str], bool] = {}
        yield InMemoryRuleConditionFixture(
            conditions=FakeRuleConditionPort(
                lambda code, ref: live_by_key.get((code, str(ref)), False)
            ),
            live_by_key=live_by_key,
        )
        return
    database_url: str = request.getfixturevalue("isolated_database_url")
    async with rolled_back_session(database_url) as session:
        clock = FixedClock(NOW)
        yield SqlRuleConditionFixture(
            conditions=SqlRuleConditionPort(
                rules=SqlRuleRepository(session),
                signals=SqlSignalRepository(session, cycle_id=uuid.uuid4()),
                creative_signals=SqlCreativeSignalRepository(session, cycle_id=uuid.uuid4()),
                freshness=SqlFreshnessPort(session, clock),
            ),
            session=session,
        )


class PlatformFixture(Protocol):
    """Fabricas de los dos puertos de plataforma en cada estado que el
    contrato necesita. Son fabricas y no atributos porque los dobles se
    configuran al construirse."""

    failure_error: type[Exception]

    def reader_reporting(self, entity_ref: EntityRef, state_hash: str) -> PlatformReaderPort: ...

    def unreachable_reader(self) -> PlatformReaderPort: ...

    def writer_confirming(
        self, proposals: ProposalRepository, state_hash: str
    ) -> AdsPlatformWritePort: ...

    def writer_denying(self, proposals: ProposalRepository) -> AdsPlatformWritePort: ...


@dataclass(slots=True)
class InMemoryPlatformFixture:
    failure_error: type[Exception] = ConnectionError

    def reader_reporting(self, entity_ref: EntityRef, state_hash: str) -> PlatformReaderPort:
        return FakePlatformReaderPort(state_hash_by_entity={str(entity_ref): state_hash})

    def unreachable_reader(self) -> PlatformReaderPort:
        return FakePlatformReaderPort(fail=True)

    def writer_confirming(
        self,
        proposals: ProposalRepository,  # noqa: ARG002 - el doble no lee propuestas
        state_hash: str,
    ) -> AdsPlatformWritePort:
        return FakeAdsPlatformWritePort(confirmed_state_hash=state_hash)

    def writer_denying(
        self,
        proposals: ProposalRepository,  # noqa: ARG002 - el doble no lee propuestas
    ) -> AdsPlatformWritePort:
        return FakeAdsPlatformWritePort(fail_with=PlatformWriteDeniedError("NO_WRITE_PATH_IN_F1"))


@dataclass(slots=True)
class BrokerPlatformFixture:
    """Los adaptadores reales sobre un `AdsPlatformPort` de contornos: el
    socket no entra en un banco de contrato, pero el mapeo de veredictos y el
    calculo del hash de estado si."""

    failure_error: type[Exception] = BrokerConnectionError

    def reader_reporting(self, entity_ref: EntityRef, state_hash: str) -> PlatformReaderPort:
        del state_hash  # el hash lo calcula el adaptador desde el estado remoto
        return BrokerPlatformReader(
            StubPlatform(entity_states={entity_ref: _entity_state(entity_ref)})
        )

    def unreachable_reader(self) -> PlatformReaderPort:
        return BrokerPlatformReader(StubPlatform(unreachable=True))

    def writer_confirming(
        self, proposals: ProposalRepository, state_hash: str
    ) -> AdsPlatformWritePort:
        return BrokerPlatformWriter(
            StubPlatform(
                outcome=WriteOutcome(
                    outcome="SUCCEEDED",
                    applied_value=Money.of("70").to_canonical(),
                    state_hash_after=state_hash,
                    error_code=None,
                    platform_request_id="req-1",
                )
            ),
            proposals,
        )

    def writer_denying(self, proposals: ProposalRepository) -> AdsPlatformWritePort:
        return BrokerPlatformWriter(StubPlatform(), proposals)


class StubPlatform(FakeAdsPlatformPort):
    """`AdsPlatformPort` de contornos: sin socket, sin SDK. Por defecto
    deniega la escritura, que es lo que hace el broker hoy."""

    def __init__(
        self,
        *,
        entity_states: dict[EntityRef, EntityStateSnapshot] | None = None,
        outcome: WriteOutcome | None = None,
        unreachable: bool = False,
    ) -> None:
        super().__init__(entity_states=entity_states)
        self._outcome = outcome
        self._unreachable = unreachable
        self.write_calls: list[WriteIntent] = []

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        if self._unreachable:
            raise BrokerConnectionError("no se pudo conectar con el broker")
        return await super().read_entity_state(entity_ref)

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        self.write_calls.append(intent)
        if self._outcome is not None:
            return self._outcome
        return await super().execute_write(intent, authorization, idempotency_key)


def _entity_state(entity_ref: EntityRef) -> EntityStateSnapshot:
    return EntityStateSnapshot(
        entity_ref=entity_ref,
        status=AdEntityStatus.ACTIVE,
        is_controllable=True,
        canonical_state={"status": "ACTIVE", "daily_budget_minor": 10_000},
        fetched_at=NOW,
    )


def remote_state_hash(entity_ref: EntityRef) -> str:
    """El hash que el adaptador real calculara para `_entity_state`."""
    return PlatformStateHash.compute(_entity_state(entity_ref).canonical_state).value


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("broker", id="broker"),
    ]
)
def platform(request: pytest.FixtureRequest) -> PlatformFixture:
    if request.param == "in_memory":
        return InMemoryPlatformFixture()
    return BrokerPlatformFixture()
