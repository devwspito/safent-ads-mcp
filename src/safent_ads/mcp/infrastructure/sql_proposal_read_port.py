"""`ProposalReadPort` real (T044/T149) sobre `proposals`
(`SqlProposalRepository`/`ProposalLens`, ya construidos por la lane
`us2-sqlrepos` para el panel/`MaintenanceCycle`). Reemplaza
`mcp.infrastructure.placeholder_ports.NotYetWiredProposalReadPort`.

Mismo patron que `sql_rule_read_port.SqlRuleReadPort`: sesion propia por
llamada, reutiliza los repositorios SQL reales de `proposals` donde la
forma ya encaja.

Limitaciones documentadas (Assumption): `ProposalLens` no tiene cursor de
paginacion (la cola de un negocio es pequena -- presupuesto de atencion
<=10/dia, FR-19) ni filtro por `cause_key` en SQL; `cause_key` se filtra en
Python tras traer el lente. `next_cursor` es siempre `None`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.execution.domain.guardrails import GuardrailScope, GuardrailSet, ScopeKind
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.mcp.application.dto import (
    Evidence as EvidenceDto,
)
from safent_ads.mcp.application.dto import (
    Page,
    ProposalDetail,
    ProposalSummary,
)
from safent_ads.mcp.application.dto import (
    ProposalState as DtoProposalState,
)
from safent_ads.mcp.application.dto import (
    ProposedDiff as ProposedDiffDto,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.proposals.domain.diff_hash import to_jsonable
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.proposal import Proposal
from safent_ads.proposals.infrastructure.sql_proposal_repository import (
    LIVE_STATES,
    ProposalLens,
    SqlProposalRepository,
)
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.read_models.dto import Money as MoneyDto

__all__ = ["SqlProposalReadPort"]

# Cadenas legibles, no la instancia entera -- esto es una lista de "que
# aplica", no un objeto que el llamador tenga que volver a interpretar.
def _guardrails_applicable(effective: GuardrailSet) -> list[str]:
    return [
        f"tope_diario={effective.daily_cap}",
        f"tope_mensual={effective.monthly_cap}",
        f"suelo={effective.floor}",
        f"techo={effective.ceiling}",
        f"salto_maximo_pct={effective.max_step_pct}",
        f"cambios_max_dia={effective.max_changes_per_entity_day}",
    ]


class SqlProposalReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_proposals(
        self,
        business_id: str,
        *,
        state: str | None,
        cause_key: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[ProposalSummary]:
        del cursor  # ver docstring del modulo: sin paginacion todavia
        states = (state,) if state else LIVE_STATES
        lens = ProposalLens(
            business_id=BusinessId.parse(business_id), states=states, limit=limit
        )
        async with self._session_factory() as session:
            proposals = await SqlProposalRepository(session).list_by_lens(lens)
        if cause_key is not None:
            proposals = tuple(
                p for p in proposals if p.cause_key.as_grouping_key() == cause_key
            )
        return Page(items=[_summary(p) for p in proposals], cursor=None)

    async def get_proposal(self, business_id: str, proposal_id: str) -> ProposalDetail:
        async with self._session_factory() as session:
            proposal = await SqlProposalRepository(session).get(
                ProposalId.parse(proposal_id)
            )
            if proposal is None or str(proposal.business_id) != business_id:
                raise EntityNotFoundError(f"{proposal_id} aun no disponible")
            guardrails_applicable = await self._guardrails_applicable(session, proposal)
        return ProposalDetail(
            summary=_summary(proposal),
            evidence=[
                EvidenceDto(
                    metric=item.metric,
                    actual=item.actual,
                    target=item.target,
                    window_label=item.window_preset,
                )
                for item in proposal.evidence
            ],
            signal_id=proposal.cause.signal_id,
            rule_id=proposal.cause.rule_id,
            guardrails_applicable=guardrails_applicable,
        )

    async def _guardrails_applicable(self, session: AsyncSession, proposal: Proposal) -> list[str]:
        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(proposal.diff.entity_ref))
        try:
            effective = await SqlGuardrailSetRepository(session).get_effective(scope)
        except LookupError:
            # Sin fila que alcance a esta entidad: mismo "denegar por
            # defecto" que el chokepoint -- no hay guardarraíl que listar,
            # no que fue una excepcion tecnica.
            return []
        return _guardrails_applicable(effective)


def _summary(proposal: Proposal) -> ProposalSummary:
    diff = proposal.diff
    return ProposalSummary(
        proposal_id=str(proposal.proposal_id),
        entity_ref=str(diff.entity_ref),
        diff=ProposedDiffDto(
            parametro=diff.parameter,
            valor_actual=_stringify(diff.before),
            valor_propuesto=_stringify(diff.after),
            diff_hash=diff.diff_hash,
        ),
        classification=proposal.classification.value,
        urgency=proposal.priority.urgency.value,
        estimated_impact=MoneyDto(
            amount=proposal.estimated_impact.amount, currency=proposal.estimated_impact.currency
        ),
        expires_at=proposal.expires_at,
        estado=DtoProposalState(proposal.state.value),
    )


def _stringify(value: object) -> str:
    return str(to_jsonable(value))
