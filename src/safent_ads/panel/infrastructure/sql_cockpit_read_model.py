"""`SqlCockpitReadModel` (026, tasks.md T007/T012): implementa
`CockpitReadPort` COMPONIENDO los read models existentes -- `PanelReadPort`
(cartera, badges, señales), `SqlProposalRepository` (propuesta viva por
fila, T010) y `ExecutionReadPort` (aplicado autónomo). Cero SQL nuevo salvo
lo que ningún read model expone hoy (franja de cambios, T012): ahí sí hace
falta una consulta propia sobre `signals`/`executions`, documentada en
`get_changes_since`.

Limitaciones deliberadas, documentadas para quien las levante despues
(honestidad de FR-009 antes que una integración a medias que fabrique un
dato):

- `customers`/`customer_value`/`roi` (spec 027 T017): pasan de
  `no_customer_source` a un valor real en cuanto el negocio tiene al menos
  un `Customer` con `first_paid_conversion_at` (cartera) o al menos un
  cliente atribuido a esa fila (`entity_ref`) -- fuente: `crm.customers`/
  `crm.revenue_events` (`RevenueEventRepository.cohort_stats`), NUNCA
  `metrics` (no trae `conversion_value` todavía). `roi_basis` pasa de
  `revenue` a `contribution` en cuanto hay dato de cartera.
- `roas` sigue en `no_data`: necesita ingreso por PLATAFORMA
  (`conversion_value` de `metrics`), una fuente distinta de la
  contribución neta del CRM -- fuera de alcance de T014-T017.
- `expected_contribution_delta` en `no_data`: `optimization.MarginalEstimate`
  es un ratio adimensional (mContribution), no un importe de dinero -- no
  hay base honesta para convertirlo a `Money` sin una propuesta de gasto
  concreta asociada.
- `pacing_index_pct` por fila reusa el ritmo de CARTERA (`caps_and_pacing`
  ya documenta "los topes/ritmo se leen de la PRIMERA cuenta publicitaria
  del negocio"): no hay ritmo por entidad en el read model actual, y
  llamar `get_pacing()` por fila multiplicaría las consultas sin una
  fuente mas fina detras.
- `freshness` por fila reusa la de cartera (peor caso entre cuentas):
  `PanelReadPort.get_freshness` no expone `platform_account_id` en su
  forma de retorno hoy."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.crm.application.customer_ports import (
    CrmBridgeHealthRepository,
    CustomerRepository,
    RevenueEventRepository,
)
from safent_ads.crm.infrastructure.sql_crm_bridge_health_repository import (
    SqlCrmBridgeHealthRepository,
)
from safent_ads.crm.infrastructure.sql_customer_repository import SqlCustomerRepository
from safent_ads.crm.infrastructure.sql_revenue_event_repository import SqlRevenueEventRepository
from safent_ads.execution.application.execution_read_port import ExecutionView
from safent_ads.execution.infrastructure.sql_execution_read_port import SqlExecutionReadPort
from safent_ads.panel.application.cockpit_dto import (
    AppliedChange,
    BrakeState,
    ChangeEntry,
    ChangeKind,
    ChangeStrip,
    CockpitView,
    CockpitWindow,
    CostPerLead,
    DetailRef,
    LeadsAndCustomers,
    PortfolioHeader,
    ProposalCounts,
    RoiBasis,
    RowSignal,
    SignalKind,
    TickerRow,
)
from safent_ads.panel.application.cockpit_dto import (
    BrakeMode as CockpitBrakeMode,
)
from safent_ads.panel.application.dto import Badges, PortfolioRow, PortfolioView, SignalView
from safent_ads.panel.application.row_action import ResolvedProposal, derive_row_action
from safent_ads.panel.infrastructure.sql_read_model import SqlPanelReadPort
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.proposals.infrastructure.sql_proposal_repository import (
    LIVE_STATES,
    ProposalLens,
    SqlProposalRepository,
)
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.read_models.dto import Measure, MeasureStatus, Money

_WINDOW_TO_PANEL: Mapping[CockpitWindow, str] = {
    CockpitWindow.TODAY: "TODAY",
    CockpitWindow.SEVEN_DAYS: "7D",
    CockpitWindow.THIRTY_DAYS: "30D",
}
_RESOLVED_UNEXECUTED_STATES = frozenset(
    {
        ProposalState.REJECTED.value,
        ProposalState.EXPIRED.value,
        ProposalState.INVALIDATED.value,
        ProposalState.FAILED.value,
    }
)
_ROW_ACTION_STATES: tuple[str, ...] = (
    *LIVE_STATES,
    ProposalState.EXECUTING.value,
    ProposalState.EXECUTED.value,
    *_RESOLVED_UNEXECUTED_STATES,
)
_EXECUTION_CORRELATION_WINDOW = timedelta(days=30)
_DEFAULT_CHANGES_SINCE_WINDOW = timedelta(days=1)
_NO_CUSTOMER_SOURCE_REASON = "Fuente de clientes no conectada (llega en la spec 027)."
_NO_REVENUE_SOURCE_REASON = "Sin ingreso de plataforma en metrics todavia."
_NO_NEW_CUSTOMERS_REASON = "Sin clientes nuevos en la ventana."
_NO_SPEND_REASON = "Sin gasto en la ventana con el que comparar."
_CENTS_PER_UNIT = Decimal(100)
_HEADER_CUSTOMERS_WEEK_DAYS = 7


def _no_customer_source[T]() -> Measure[T]:
    return Measure.unavailable(MeasureStatus.NO_CUSTOMER_SOURCE, reason=_NO_CUSTOMER_SOURCE_REASON)


def _no_revenue_source[T]() -> Measure[T]:
    return Measure.unavailable(MeasureStatus.NO_DATA, reason=_NO_REVENUE_SOURCE_REASON)


class SqlCockpitReadModel:
    """Una instancia por peticion HTTP (mismo patron que `SqlPanelReadPort`
    en `panel/presentation/rest.py`)."""

    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()
        self._panel = SqlPanelReadPort(session, self._clock)
        self._proposals = SqlProposalRepository(session)
        self._executions = SqlExecutionReadPort(session)
        self._customers: CustomerRepository = SqlCustomerRepository(session)
        self._revenue_events: RevenueEventRepository = SqlRevenueEventRepository(session)
        self._crm_bridge_health: CrmBridgeHealthRepository = SqlCrmBridgeHealthRepository(session)

    async def get_cockpit(self, business_id: str, *, window: str) -> CockpitView:
        cockpit_window = CockpitWindow(window)
        primary = await self._panel.get_portfolio(
            business_id, window=_WINDOW_TO_PANEL[cockpit_window]
        )
        today = await self._portfolio_for(business_id, cockpit_window, CockpitWindow.TODAY, primary)
        week = await self._portfolio_for(
            business_id, cockpit_window, CockpitWindow.SEVEN_DAYS, primary
        )
        badges = await self._panel.get_badges(business_id, signals_since=None)
        # Spec 027 T017: distingue "sin puente CRM configurado" (sigue
        # `NO_CUSTOMER_SOURCE`, honesto de por vida hasta que el dueno
        # enlace un conector) de "puente configurado, todavia sin datos en
        # esta ventana/fila" (`NO_DATA`, mismo criterio que `_lead_count`).
        # Calculado UNA vez por peticion, nunca por fila.
        bridge_configured = await self._is_bridge_configured(business_id)

        signals_by_entity = await self._latest_signal_by_entity(business_id)
        resolved_by_entity = await self._resolved_proposals_by_entity(business_id)
        # FR-006: orden por defecto = dinero en juego descendente.
        # `PortfolioView.rows` viene ordenado por nombre (GET /portfolio no
        # lo necesita); el cockpit reordena en memoria, sin tocar esa
        # consulta ni el contrato de `/portfolio` (SC-003).
        ordered_rows = sorted(primary.rows, key=lambda row: row.money_at_stake.amount, reverse=True)
        rows = tuple(
            [
                await self._ticker_row(
                    business_id,
                    row,
                    primary,
                    signals_by_entity=signals_by_entity,
                    resolved_by_entity=resolved_by_entity,
                    badges=badges,
                    bridge_configured=bridge_configured,
                )
                for row in ordered_rows
            ]
        )
        changes_since = await self.get_changes_since(
            business_id, since=self._clock.now() - _DEFAULT_CHANGES_SINCE_WINDOW
        )

        return CockpitView(
            business_id=business_id,
            window=cockpit_window,
            currency=primary.currency,
            generated_at=self._clock.now(),
            freshness=primary.freshness,
            is_partial=primary.is_partial,
            degraded_accounts=tuple(primary.degraded_accounts),
            header=await self._header(
                business_id, primary, today, week, badges, bridge_configured=bridge_configured
            ),
            rows=rows,
            changes_since=changes_since,
        )

    async def _portfolio_for(
        self,
        business_id: str,
        selected: CockpitWindow,
        target: CockpitWindow,
        primary: PortfolioView,
    ) -> PortfolioView:
        if selected is target:
            return primary
        return await self._panel.get_portfolio(business_id, window=_WINDOW_TO_PANEL[target])

    async def _header(
        self,
        business_id: str,
        primary: PortfolioView,
        today: PortfolioView,
        week: PortfolioView,
        badges: Badges,
        *,
        bridge_configured: bool,
    ) -> PortfolioHeader:
        roi, roi_basis, roi_basis_reason = await self._header_roi(
            business_id, primary, bridge_configured=bridge_configured
        )
        return PortfolioHeader(
            spend=primary.spend,
            caps=primary.caps,
            pacing=primary.pacing,
            projected_month_end=self._projected_month_end(primary),
            leads=LeadsAndCustomers(
                today=self._lead_count(today), week=self._lead_count(week)
            ),
            customers=LeadsAndCustomers(
                today=await self._customer_count_header(
                    business_id, CockpitWindow.TODAY, bridge_configured=bridge_configured
                ),
                week=await self._customer_count_header(
                    business_id, CockpitWindow.SEVEN_DAYS, bridge_configured=bridge_configured
                ),
            ),
            roi=roi,
            roi_basis=roi_basis,
            roi_basis_reason=roi_basis_reason,
            roas=_no_revenue_source(),
            cost_per_lead=self._cost_per_lead(primary.cost_per_lead),
            brake=BrakeState(
                engaged=badges.brake.engaged,
                mode=CockpitBrakeMode(badges.brake.mode.value) if badges.brake.mode else None,
                since=None,
            ),
            proposals=ProposalCounts(
                pending=badges.proposals.pending,
                deferred=badges.proposals.deferred,
                critical=badges.proposals.critical,
            ),
        )

    async def _is_bridge_configured(self, business_id: str) -> bool:
        health = await self._crm_bridge_health.get_for_business(
            business_id=BusinessId(uuid.UUID(business_id))
        )
        return health is not None

    def _window_bounds(self, window: CockpitWindow) -> tuple[datetime, datetime]:
        now = self._clock.now()
        if window is CockpitWindow.TODAY:
            start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
            return start, start + timedelta(days=1)
        return now - timedelta(days=_HEADER_CUSTOMERS_WEEK_DAYS), now + timedelta(days=1)

    async def _customer_count_header(
        self, business_id: str, window: CockpitWindow, *, bridge_configured: bool
    ) -> Measure[int]:
        if not bridge_configured:
            return _no_customer_source()
        start, end = self._window_bounds(window)
        count = await self._customers.count_active_in_window(
            business_id=BusinessId(uuid.UUID(business_id)),
            entity_ref=None,
            window_start=start,
            window_end=end,
        )
        if count <= 0:
            return Measure.unavailable(MeasureStatus.NO_DATA, reason=_NO_NEW_CUSTOMERS_REASON)
        return Measure.available(count)

    async def _header_roi(
        self, business_id: str, primary: PortfolioView, *, bridge_configured: bool
    ) -> tuple[Measure[float], RoiBasis, str | None]:
        if not bridge_configured:
            return _no_customer_source(), RoiBasis.REVENUE, _NO_CUSTOMER_SOURCE_REASON
        stats = await self._revenue_events.cohort_stats(
            business_id=BusinessId(uuid.UUID(business_id)), entity_ref=None
        )
        if stats.cohort_size <= 0:
            no_customers_yet: Measure[float] = Measure.unavailable(
                MeasureStatus.NO_DATA, reason=_NO_NEW_CUSTOMERS_REASON
            )
            return no_customers_yet, RoiBasis.CONTRIBUTION, None
        spend = primary.spend.window.amount
        if spend <= 0:
            no_spend: Measure[float] = Measure.unavailable(
                MeasureStatus.NO_DATA, reason=_NO_SPEND_REASON
            )
            return no_spend, RoiBasis.CONTRIBUTION, None
        contribution = Decimal(stats.observed_contribution_minor) / _CENTS_PER_UNIT
        return Measure.available(float(contribution / spend)), RoiBasis.CONTRIBUTION, None

    def _lead_count(self, view: PortfolioView) -> Measure[int]:
        leads = view.conversions_by_kind.get("lead", 0)
        if leads <= 0:
            return Measure.unavailable(MeasureStatus.NO_DATA, reason="Sin leads en la ventana.")
        return Measure.available(leads)

    def _cost_per_lead(self, actual: Money | None) -> CostPerLead:
        return CostPerLead(
            actual=(
                Measure.available(actual)
                if actual is not None
                else Measure.unavailable(MeasureStatus.NO_DATA, reason="Sin leads en la ventana.")
            ),
            target=Measure.unavailable(
                MeasureStatus.NO_DATA,
                reason="Perfil de economia unitaria no vinculado a esta entidad todavia.",
            ),
            delta_pct=Measure.unavailable(
                MeasureStatus.NO_DATA, reason="Sin coste objetivo con el que comparar."
            ),
        )

    def _projected_month_end(self, primary: PortfolioView) -> Measure[Money]:
        if primary.is_partial or primary.caps.monthly is None:
            return Measure.unavailable(
                MeasureStatus.NO_DATA, reason="Sin guardarrail configurado."
            )
        # `caps_and_pacing` ya calculó `projection_pct` a partir de una
        # proyeccion en dinero que no expone -- se recompone aqui como
        # `mtd x (projection_pct / index_pct)` solo cuando ambos son
        # positivos; en cualquier otro caso, honesto "sin dato" en vez de
        # dividir por cero o inventar una proyeccion.
        index = primary.pacing.index_pct
        if index <= 0:
            return Measure.unavailable(MeasureStatus.NO_DATA, reason="Sin ritmo de gasto todavia.")
        ratio = Decimal(str(primary.pacing.projection_pct / index))
        projected = (primary.spend.mtd.amount * ratio).quantize(Decimal("0.01"))
        return Measure.available(Money(projected, primary.spend.mtd.currency))

    async def _latest_signal_by_entity(self, business_id: str) -> dict[str, SignalView]:
        page = await self._panel.list_signals(
            business_id,
            kind=None,
            min_strength=None,
            since=None,
            platform=None,
            entity_ref=None,
            limit=200,
            cursor=None,
        )
        latest: dict[str, SignalView] = {}
        for item in page.items:  # ya viene ORDER BY emitted_at DESC
            latest.setdefault(item.entity_ref, item)
        return latest

    async def _resolved_proposals_by_entity(
        self, business_id: str
    ) -> dict[str, ResolvedProposal | None]:
        proposals = await self._proposals.list_by_lens(
            ProposalLens(
                business_id=BusinessId(uuid.UUID(business_id)),
                states=_ROW_ACTION_STATES,
                limit=200,
            )
        )
        executions_by_proposal = await self._executions_by_proposal_id(business_id)
        by_entity: dict[str, list[Proposal]] = {}
        for proposal in proposals:
            by_entity.setdefault(str(proposal.diff.entity_ref), []).append(proposal)
        return {
            entity_ref: _resolve_proposal(candidates, executions_by_proposal)
            for entity_ref, candidates in by_entity.items()
        }

    async def _executions_by_proposal_id(self, business_id: str) -> dict[str, ExecutionView]:
        since = self._clock.now() - _EXECUTION_CORRELATION_WINDOW
        executions = await self._executions.list_for_business(
            business_id, outcome=None, since=since, limit=500
        )
        return {execution.proposal_id: execution for execution in executions}

    async def _ticker_row(
        self,
        business_id: str,
        row: PortfolioRow,
        primary: PortfolioView,
        *,
        signals_by_entity: Mapping[str, SignalView],
        resolved_by_entity: Mapping[str, ResolvedProposal | None],
        badges: Badges,
        bridge_configured: bool,
    ) -> TickerRow:
        signal_view = signals_by_entity.get(row.entity_ref)
        signal_kind = SignalKind(row.signal.kind.upper()) if row.signal else None
        proposal = resolved_by_entity.get(row.entity_ref)
        action = derive_row_action(
            signal_kind=signal_kind,
            is_controllable=row.is_controllable,
            is_stale=primary.freshness.is_stale,
            brake_engaged=badges.brake.engaged,
            proposal=proposal,
            now=self._clock.now(),
        )
        customers, customer_value, roi = await self._row_customer_measures(
            business_id, row, bridge_configured=bridge_configured
        )
        return TickerRow(
            entity_ref=row.entity_ref,
            name=row.name,
            level="campaign",
            platform=row.platform,
            platform_account_id=row.platform_account_id,
            status=row.status,
            signal=self._row_signal(row, signal_view),
            money_at_stake=row.money_at_stake,
            expected_contribution_delta=Measure.unavailable(
                MeasureStatus.NO_DATA,
                reason="Estimacion marginal no disponible en el cockpit todavia.",
            ),
            roi=roi,
            roas=_no_revenue_source(),
            leads=Measure.unavailable(
                MeasureStatus.NO_DATA, reason="Leads por entidad no expuestos todavia."
            ),
            customers=customers,
            customer_value=customer_value,
            cost_per_lead=self._cost_per_lead(row.cost_per_lead),
            spend=row.spend,
            cap=None,
            pacing_index_pct=(
                Measure.available(primary.pacing.index_pct)
                if not primary.is_partial
                else Measure.unavailable(
                    MeasureStatus.NO_DATA, reason="Sin guardarrail configurado."
                )
            ),
            sparkline=tuple(row.spend_14d),
            freshness=primary.freshness,
            is_controllable=row.is_controllable,
            is_degraded=row.is_degraded,
            learning_state=row.learning_state,
            action=action,
        )

    async def _row_customer_measures(
        self, business_id: str, row: PortfolioRow, *, bridge_configured: bool
    ) -> tuple[Measure[int], Measure[Money], Measure[float]]:
        """Spec 027 T017: una consulta por fila (N+1 documentado, mismo
        criterio que `pacing_index_pct`/`freshness` reusando cartera en
        este mismo fichero -- el read model existente no expone un
        agregado por entidad de otro contexto en lote)."""
        if not bridge_configured:
            return _no_customer_source(), _no_customer_source(), _no_customer_source()
        stats = await self._revenue_events.cohort_stats(
            business_id=BusinessId(uuid.UUID(business_id)), entity_ref=row.entity_ref
        )
        if stats.cohort_size <= 0:
            no_customers: Measure[int] = Measure.unavailable(
                MeasureStatus.NO_DATA, reason="Sin clientes atribuidos a esta entidad todavia."
            )
            no_value: Measure[Money] = Measure.unavailable(
                MeasureStatus.NO_DATA, reason="Sin clientes atribuidos a esta entidad todavia."
            )
            no_roi: Measure[float] = Measure.unavailable(
                MeasureStatus.NO_DATA, reason="Sin clientes atribuidos a esta entidad todavia."
            )
            return no_customers, no_value, no_roi
        currency = stats.currency or row.spend.currency
        contribution = Money(Decimal(stats.observed_contribution_minor) / _CENTS_PER_UNIT, currency)
        customers = Measure.available(stats.cohort_size)
        customer_value = Measure.available(contribution)
        roi: Measure[float]
        if row.spend.amount <= 0:
            roi = Measure.unavailable(MeasureStatus.NO_DATA, reason=_NO_SPEND_REASON)
        else:
            roi = Measure.available(float(contribution.amount / row.spend.amount))
        return customers, customer_value, roi

    def _row_signal(self, row: PortfolioRow, view: SignalView | None) -> RowSignal | None:
        if row.signal is None:
            return None
        if view is None:
            # `list_signals` no trajo la vigente por otro motivo (borde de
            # paginacion): se declara con lo que hay, nunca se inventa un
            # `signal_id`.
            return None
        return RowSignal(
            signal_id=view.signal_id,
            kind=SignalKind(row.signal.kind.upper()),
            strength=row.signal.strength,
            cause=row.signal.cause,
            emitted_at=view.emitted_at,
        )

    # -- Franja de cambios (T012) -----------------------------------------

    async def get_changes_since(self, business_id: str, *, since: datetime) -> ChangeStrip:
        """`entity_entered`/`entity_exited`/`threshold_crossed` no tienen
        fuente todavia (ningun read model existente las expone) -- se
        declaran ausentes via `is_partial=True`, nunca una lista vacia que
        aparente estar completa (FR-008, tasks.md T012)."""
        signal_items = await self._signal_change_items(business_id, since)
        applied_items = await self._autonomous_applied_items(business_id, since)
        items = tuple(
            sorted(signal_items + applied_items, key=lambda item: item.occurred_at, reverse=True)
        )
        return ChangeStrip(since=since, is_partial=True, items=items)

    async def _signal_change_items(
        self, business_id: str, since: datetime
    ) -> tuple[ChangeEntry, ...]:
        page = await self._panel.list_signals(
            business_id,
            kind=None,
            min_strength=None,
            since=since,
            platform=None,
            entity_ref=None,
            limit=200,
            cursor=None,
        )
        return tuple(
            ChangeEntry(
                kind=ChangeKind.SIGNAL_CHANGED,
                entity_ref=item.entity_ref,
                entity_name=item.entity_name,
                before=None,
                after=item.kind.upper(),
                occurred_at=item.emitted_at,
                detail_ref=DetailRef(kind="signal", id=item.signal_id),
            )
            for item in page.items
        )

    async def _autonomous_applied_items(
        self, business_id: str, since: datetime
    ) -> tuple[ChangeEntry, ...]:
        executions = await self._executions.list_for_business(
            business_id, outcome="SUCCEEDED", since=since, limit=200
        )
        proposals_by_id = await self._proposals_by_id(
            business_id, {execution.proposal_id for execution in executions}
        )
        items: list[ChangeEntry] = []
        for execution in executions:
            if execution.undone_at is not None:
                continue
            proposal = proposals_by_id.get(execution.proposal_id)
            entity_ref = str(proposal.diff.entity_ref) if proposal else execution.entity_name
            items.append(
                ChangeEntry(
                    kind=ChangeKind.AUTONOMOUS_APPLIED,
                    entity_ref=entity_ref,
                    entity_name=execution.entity_name,
                    before=_stringify(execution.previous_value),
                    after=_stringify(execution.applied_value),
                    occurred_at=execution.started_at,
                    detail_ref=DetailRef(kind="execution", id=execution.execution_id),
                )
            )
        return tuple(items)

    async def _proposals_by_id(
        self, business_id: str, proposal_ids: set[str]
    ) -> dict[str, Proposal]:
        if not proposal_ids:
            return {}
        proposals = await self._proposals.list_by_lens(
            ProposalLens(
                business_id=BusinessId(uuid.UUID(business_id)),
                states=_ROW_ACTION_STATES,
                limit=200,
            )
        )
        return {
            str(proposal.proposal_id): proposal
            for proposal in proposals
            if str(proposal.proposal_id) in proposal_ids
        }


def _resolve_proposal(
    candidates: Sequence[Proposal], executions_by_proposal: Mapping[str, ExecutionView]
) -> ResolvedProposal | None:
    if not candidates:
        return None
    proposal = max(candidates, key=lambda p: p.created_at)
    return ResolvedProposal(
        proposal_id=str(proposal.proposal_id),
        diff_hash=proposal.diff.diff_hash,
        classification=proposal.classification.value,
        is_live=proposal.state.value in LIVE_STATES,
        is_resolved_unexecuted=proposal.state.value in _RESOLVED_UNEXECUTED_STATES,
        applied_change=_applied_change(proposal, executions_by_proposal),
        execution_id=_execution_id(proposal, executions_by_proposal),
    )


def _applied_change(
    proposal: Proposal, executions_by_proposal: Mapping[str, ExecutionView]
) -> AppliedChange | None:
    if proposal.state is not ProposalState.EXECUTED:
        return None
    execution = executions_by_proposal.get(str(proposal.proposal_id))
    if execution is None or execution.outcome != "SUCCEEDED" or execution.undone_at is not None:
        return None
    return AppliedChange(
        parameter=proposal.diff.parameter,
        before=_stringify(execution.previous_value),
        after=_stringify(execution.applied_value),
        applied_at=execution.started_at,
        undo_deadline=execution.undo_deadline or execution.started_at,
    )


def _execution_id(
    proposal: Proposal, executions_by_proposal: Mapping[str, ExecutionView]
) -> str | None:
    execution = executions_by_proposal.get(str(proposal.proposal_id))
    return execution.execution_id if execution is not None else None


def _stringify(value: object) -> str:
    return "" if value is None else str(value)


class RequestScopedCockpitReadModel:
    """`CockpitReadPort` real, una sesion por llamada (mismo patron que
    `RequestScopedPanelReadPort`)."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def get_cockpit(self, business_id: str, *, window: str) -> CockpitView:
        async with self._session_factory() as session:
            return await SqlCockpitReadModel(session, self._clock).get_cockpit(
                business_id, window=window
            )

    async def get_changes_since(self, business_id: str, *, since: datetime) -> ChangeStrip:
        async with self._session_factory() as session:
            return await SqlCockpitReadModel(session, self._clock).get_changes_since(
                business_id, since=since
            )
