"""Contenedor de dependencias de `ads-api`/`ads-worker`: cablea settings con
motor async de SQLAlchemy, los puertos N0 (`Clock`, `IdGenerator`) y las
fabricas por-sesion que el resto de `composition/` usa para construir los
casos de uso reales.

Raiz de composicion plana (auditoria de simplificacion §12): `Container`
solo declara lo que vive UNA vez por proceso -- motor, `session_factory`,
reloj, generador de ids, el puerto de plataforma y el par de claves de
aprobacion. Todo lo que necesita una transaccion (`proposals`/`execution`:
`ExecutionChokepoint`, `AuthorizeRuleAction`, `UndoExecution`,
`ToggleEmergencyBrake`) se construye PER SESSION via
`build_execution_use_cases()`, dentro de `async with session_factory() as
session`; el llamador hace `await session.commit()` cuando termina (el
`SqlUnitOfWork` que usa el chokepoint solo confirma la transaccion del
reclamo/guardarraíl -- ver su docstring)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.composition.database import Database
from safent_ads.composition.settings import CommonSettings
from safent_ads.composition.signing import ApprovalKeyPair, build_approval_key_pair
from safent_ads.execution.application.apply_defensive_action import ApplyDefensiveAction
from safent_ads.execution.application.authorize_rule_action import AuthorizeRuleAction
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.entity_lifecycle_actions import (
    DeleteEntity,
    EntityLifecycleDeps,
    PauseEntity,
    ResumeEntity,
)
from safent_ads.execution.application.platform_state_revalidator import (
    PlatformStateRevalidator,
)
from safent_ads.execution.application.toggle_emergency_brake import ToggleEmergencyBrake
from safent_ads.execution.application.undo_execution import UndoExecution
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.infrastructure.broker_platform import (
    BrokerPlatformReader,
    BrokerPlatformWriter,
)
from safent_ads.execution.infrastructure.decision_recorder import SqlDecisionRecorder
from safent_ads.execution.infrastructure.sql_brake_state import (
    DEFAULT_BRAKE_ACTOR,
    SqlBrakeStatePort,
)
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_execution_reservations import SqlExecutionReservations
from safent_ads.execution.infrastructure.sql_freshness import SqlFreshnessPort
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.execution.infrastructure.sql_rule_condition import SqlRuleConditionPort
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.execution.infrastructure.sql_unit_of_work import SqlUnitOfWork
from safent_ads.iam.infrastructure.enterprise_human_approval import EnterpriseHumanApproval
from safent_ads.mcp.infrastructure.broker_gaql_read_port import BrokerGaqlReadPort
from safent_ads.mcp.infrastructure.sql_audit_read_port import SqlAuditReadPort
from safent_ads.mcp.infrastructure.sql_catalog_read_port import SqlCatalogReadPort
from safent_ads.mcp.infrastructure.sql_entity_read_port import SqlEntityReadPort
from safent_ads.mcp.infrastructure.sql_portfolio_read_port import SqlPortfolioReadPort
from safent_ads.mcp.infrastructure.sql_proposal_read_port import SqlProposalReadPort
from safent_ads.mcp.infrastructure.sql_rule_read_port import SqlRuleReadPort
from safent_ads.mcp.infrastructure.sql_signal_read_port import SqlSignalReadPort
from safent_ads.proposals.application.propose_action import ProposeAction
from safent_ads.proposals.application.submit_approval import SubmitApproval
from safent_ads.proposals.application.withdraw_proposal import WithdrawProposal
from safent_ads.proposals.domain.authorization import AuthorizationVerifier
from safent_ads.proposals.domain.classification import ClassificationPolicy
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy
from safent_ads.proposals.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRepository,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import IdGenerator, UuidIdGenerator
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlCreativeSignalRepository,
    SqlSignalRepository,
)

__all__ = ["Container", "ExecutionUseCases"]

# `SqlSignalRepository`/`SqlCreativeSignalRepository` piden un `cycle_id`
# para `save()`; `SqlRuleConditionPort` solo los usa para leer
# (`find_latest_for_entity`), igual que ya hace `SqlRuleReadPort.explain_rule`
# con el mismo marcador -- un intento de guardar con el sin querer se veria
# en la primera prueba de contrato, no en produccion en silencio.
_READ_ONLY_CYCLE_ID = uuid.UUID(int=0)

# FR-12: toda propuesta cuyo impacto estimado alcance esto escala a
# `CRITICAL` independientemente de su tipo. `classification.py` (dominio) a
# proposito no fija un numero -- lo recibe como parametro de politica.
# Assumption documentada (spec.md no fija el umbral): 2000 EUR, pendiente de
# confirmacion del propietario. Cambiarlo es un valor, no un redisenio.
_CRITICAL_IMPACT_THRESHOLD = Money.of("2000")


@dataclass(slots=True)
class Container:
    """Dependencias de proceso compartidas por `ads-api` y `ads-worker`."""

    settings: CommonSettings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    id_generator: IdGenerator
    ads_platform_port: AdsPlatformPort
    approval_key_pair: ApprovalKeyPair
    mcp_portfolio_read_port: SqlPortfolioReadPort
    mcp_entity_read_port: SqlEntityReadPort
    mcp_signal_read_port: SqlSignalReadPort
    mcp_rule_read_port: SqlRuleReadPort
    mcp_audit_read_port: SqlAuditReadPort
    mcp_gaql_read_port: BrokerGaqlReadPort
    mcp_catalog_read_port: SqlCatalogReadPort
    mcp_proposal_read_port: SqlProposalReadPort
    managed_human_authority: EnterpriseHumanApproval | None = None

    @classmethod
    def build(cls, settings: CommonSettings) -> Container:
        database = Database.from_settings(settings)
        clock = SystemClock()
        return cls(
            settings=settings,
            engine=database.engine,
            session_factory=database.session_factory,
            clock=clock,
            id_generator=UuidIdGenerator(),
            ads_platform_port=BrokerSocketClient(settings.broker_socket_path),
            approval_key_pair=build_approval_key_pair(
                settings.approval_signing_key.get_secret_value()
            ),
            mcp_portfolio_read_port=SqlPortfolioReadPort(database.session_factory, clock),
            mcp_entity_read_port=SqlEntityReadPort(database.session_factory, clock),
            mcp_signal_read_port=SqlSignalReadPort(database.session_factory, clock),
            mcp_rule_read_port=SqlRuleReadPort(database.session_factory),
            mcp_audit_read_port=SqlAuditReadPort(database.session_factory),
            mcp_gaql_read_port=BrokerGaqlReadPort(
                BrokerSocketClient(settings.broker_socket_path), database.session_factory
            ),
            mcp_catalog_read_port=SqlCatalogReadPort(database.session_factory, clock),
            mcp_proposal_read_port=SqlProposalReadPort(database.session_factory),
            managed_human_authority=(
                EnterpriseHumanApproval(settings.managed_trust())
                if settings.managed_central else None
            ),
        )

    def build_execution_use_cases(
        self, session: AsyncSession, *, brake_actor: str = DEFAULT_BRAKE_ACTOR
    ) -> ExecutionUseCases:
        """Cablea `ExecutionChokepoint`/`ToggleEmergencyBrake`/
        `AuthorizeRuleAction`/`UndoExecution` sobre adaptadores SQL reales,
        TODOS atados a `session` -- el reclamo de la cola y la evaluacion
        del guardarraíl tienen que caber en la misma transaccion
        (threat-model.md C-15, `SqlUnitOfWork`). Llamar una vez por sesion,
        nunca reusar entre peticiones/ciclos.

        `brake_actor`: quien pulsa el freno (REST `/kill-switch` pasa el
        propietario autenticado; `RuleCycle`/lo demas se queda con
        `DEFAULT_BRAKE_ACTOR`)."""
        proposals = SqlProposalRepository(session)
        authorizations = SqlAuthorizationRepository(session)
        brakes = SqlBrakeStatePort(session, actor=brake_actor)
        execution_queue = SqlExecutionQueue(session, self.clock)
        guardrail_sets = SqlGuardrailSetRepository(session)
        spend_ledger = SqlSpendLedger(session, self.clock, execution_queue)
        guardrail_evaluator = GuardrailEvaluator()
        recorder = SqlDecisionRecorder(SqlDecisionLogRepository(session))
        signer, verifier = self.approval_key_pair.signer, self.approval_key_pair.verifier

        chokepoint = ExecutionChokepoint(
            queue=execution_queue,
            uow=SqlUnitOfWork(session),
            brakes=brakes,
            proposals=proposals,
            authorizations=authorizations,
            auth_verifier=AuthorizationVerifier(verifier, self.clock),
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            reservations=SqlExecutionReservations(session, self.clock),
            revalidator=PlatformStateRevalidator(BrokerPlatformReader(self.ads_platform_port)),
            platform_write=BrokerPlatformWriter(self.ads_platform_port, proposals),
            recorder=recorder,
            clock=self.clock,
            enabled_google_channels=self.settings.google_channels_enabled,
        )
        toggle_emergency_brake = ToggleEmergencyBrake(brakes, recorder, self.clock)
        authorize_rule_action = AuthorizeRuleAction(
            proposals=proposals,
            authorizations=authorizations,
            rule_condition=SqlRuleConditionPort(
                rules=SqlRuleRepository(session),
                signals=SqlSignalRepository(session, cycle_id=_READ_ONLY_CYCLE_ID),
                creative_signals=SqlCreativeSignalRepository(session, cycle_id=_READ_ONLY_CYCLE_ID),
                freshness=SqlFreshnessPort(session, self.clock),
            ),
            brakes=brakes,
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            signer=signer,
            clock=self.clock,
        )
        undo_execution = UndoExecution(
            proposals=proposals,
            authorizations=authorizations,
            executions=execution_queue,
            # security-review-f4.md B-2: instancia propia, misma `session`
            # que la del chokepoint -- `SqlUnitOfWork` no abre una
            # transaccion nueva si ya hay una en curso (ver su docstring),
            # y su `_depth` es por instancia, asi que no interfiere con la
            # del chokepoint.
            uow=SqlUnitOfWork(session),
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            signer=signer,
            clock=self.clock,
        )
        propose_action = ProposeAction(
            proposals=proposals,
            classification_policy=ClassificationPolicy(
                critical_impact_threshold=_CRITICAL_IMPACT_THRESHOLD
            ),
            expiry_policy=ExpiryPolicy(),
            clock=self.clock,
        )
        withdraw_proposal = WithdrawProposal(proposals, self.clock)
        submit_approval = SubmitApproval(
            proposals=proposals,
            authorizations=authorizations,
            execution_queue=execution_queue,
            brakes=brakes,
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            signer=signer,
            clock=self.clock,
            human_authority=self.managed_human_authority,
            enabled_google_channels=self.settings.google_channels_enabled,
        )
        apply_defensive_action = ApplyDefensiveAction(
            ad_entities=SqlAdEntityRepository(session),
            brakes=brakes,
            freshness=SqlFreshnessPort(session, self.clock),
            rule_condition=SqlRuleConditionPort(
                rules=SqlRuleRepository(session),
                signals=SqlSignalRepository(session, cycle_id=_READ_ONLY_CYCLE_ID),
                creative_signals=SqlCreativeSignalRepository(session, cycle_id=_READ_ONLY_CYCLE_ID),
                freshness=SqlFreshnessPort(session, self.clock),
            ),
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            proposals=proposals,
            propose_action=propose_action,
            authorize_rule_action=authorize_rule_action,
            execution_queue=execution_queue,
            chokepoint=chokepoint,
            clock=self.clock,
        )
        entity_lifecycle_deps = EntityLifecycleDeps(
            ad_entities=SqlAdEntityRepository(session),
            brakes=brakes,
            proposals=proposals,
            authorizations=authorizations,
            propose_action=propose_action,
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            execution_queue=execution_queue,
            chokepoint=chokepoint,
            signer=signer,
            clock=self.clock,
        )
        return ExecutionUseCases(
            chokepoint=chokepoint,
            toggle_emergency_brake=toggle_emergency_brake,
            authorize_rule_action=authorize_rule_action,
            undo_execution=undo_execution,
            propose_action=propose_action,
            withdraw_proposal=withdraw_proposal,
            apply_defensive_action=apply_defensive_action,
            submit_approval=submit_approval,
            pause_entity=PauseEntity(entity_lifecycle_deps),
            resume_entity=ResumeEntity(entity_lifecycle_deps),
            delete_entity=DeleteEntity(entity_lifecycle_deps),
            proposals=proposals,
            authorizations=authorizations,
            execution_queue=execution_queue,
            brakes=brakes,
            guardrail_evaluator=guardrail_evaluator,
            guardrail_sets=guardrail_sets,
            recorder=recorder,
        )

    async def aclose(self) -> None:
        if self.managed_human_authority is not None:
            await self.managed_human_authority.aclose()
        await self.engine.dispose()


@dataclass(frozen=True, slots=True)
class ExecutionUseCases:
    """Los casos de uso de `execution`/`proposals` (autonomia defensiva Y
    escritura aprobada por humano) mas los repositorios crudos que sus
    llamadores (REST, MCP, `RuleCycle`, `ExecutionCycle`) tambien necesitan
    tocar directamente (p. ej. anexar una `Proposal` nueva antes de
    autorizarla). Vive tanto como la sesion con la que se construyo --
    `Container.build_execution_use_cases`."""

    chokepoint: ExecutionChokepoint
    toggle_emergency_brake: ToggleEmergencyBrake
    authorize_rule_action: AuthorizeRuleAction
    undo_execution: UndoExecution
    propose_action: ProposeAction
    withdraw_proposal: WithdrawProposal
    apply_defensive_action: ApplyDefensiveAction
    submit_approval: SubmitApproval
    pause_entity: PauseEntity
    resume_entity: ResumeEntity
    delete_entity: DeleteEntity
    proposals: SqlProposalRepository
    authorizations: SqlAuthorizationRepository
    execution_queue: SqlExecutionQueue
    brakes: SqlBrakeStatePort
    guardrail_evaluator: GuardrailEvaluator
    guardrail_sets: SqlGuardrailSetRepository
    recorder: SqlDecisionRecorder
