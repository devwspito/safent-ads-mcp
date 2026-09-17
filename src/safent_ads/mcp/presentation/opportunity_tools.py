"""Las 2 herramientas MCP de oportunidades (tasks.md T114): `propose_campaign`
(WRITE) y `list_opportunities` (READ).

Modulo autonomo (mismo criterio de aislamiento que `experiment_tools.py`,
T201): declara sus propios `Args` sobre `mcp.presentation.args.ToolArgs`
(mismas invariantes de seguridad -- `extra=forbid`, sin URLs libres --
y `business_id` obligatorio, regla 4 del contrato) y sus propios
handlers. `catalog.py` la engancha con una linea
(`build_opportunity_tool_definitions`), sin tocar `handlers.py`/
`write_handlers.py`/`read_model_ports.py`.

`propose_campaign` estaba fuera a proposito (`catalog.py:80-82`: una
campana que aun no existe no tenia `EntityRef` -- resuelto por
`0027_us5_opportunities`, ver `opportunities.domain.opportunity_candidate`).
Nunca toca una plataforma (regla 3 del contrato); nace `pending` y, al
ejecutarse, `PAUSED` (FR-36)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, model_validator

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.dto import PlatformCode as PlatformCodeArg
from safent_ads.mcp.application.errors import (
    EntityNotFoundError,
    GuardrailBlockedError,
    ToolValidationError,
)
from safent_ads.mcp.presentation.args import BusinessId as BusinessIdStr
from safent_ads.mcp.presentation.args import EntityRefStr, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.campaign_creation_args import CampaignCreationPlanArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.opportunities.application.errors import (
    AmbiguousActiveAccountForPlatformError,
    DailyBudgetExceedsCapError,
    NoActiveAccountForPlatformError,
    OfferingNotFoundError,
)
from safent_ads.opportunities.application.list_opportunities import ListOpportunities
from safent_ads.opportunities.application.ports import CampaignProposalOutcome, OpenOpportunityView
from safent_ads.opportunities.application.propose_campaign import (
    ProposeCampaign,
    ProposeCampaignRequest,
)
from safent_ads.opportunities.domain.campaign_brief import (
    MAX_DURATION_DAYS,
    MIN_DURATION_DAYS,
    CampaignBrief,
)
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = ["OpportunityToolServices", "build_opportunity_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_MAX_TEXT_LENGTH = 280
_BUDGET_CURRENCY = "EUR"


@dataclass(frozen=True, slots=True)
class OpportunityToolServices:
    """Los 2 casos de uso de `opportunities` que necesita esta lane --
    `catalog.py` los construye una vez (misma sesion/reloj que el resto de
    `opportunities`) y se los pasa a `build_opportunity_tool_definitions`."""

    propose_campaign: ProposeCampaign
    list_opportunities: ListOpportunities


class ProposeCampaignArgs(ToolArgs):
    business_id: BusinessIdStr
    platform: PlatformCodeArg
    account_ref: EntityRefStr | None = None
    objective: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT_LENGTH)]
    offering_id: OpaqueId
    daily_budget_amount: Annotated[str, Field(pattern=r"^\d+(\.\d{1,2})?$")]
    duration_days: int = Field(ge=MIN_DURATION_DAYS, le=MAX_DURATION_DAYS)
    success_criterion: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT_LENGTH)]
    kill_criterion: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT_LENGTH)]
    angle: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT_LENGTH)]
    targeting_seed: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT_LENGTH)]
    geo: Annotated[str, Field(max_length=64)] | None = None
    calendar_event_id: OpaqueId | None = None
    creation_plan: CampaignCreationPlanArgs | None = Field(
        default=None,
        description=(
            "Plan nativo explícito opcional para crear sólo el contenedor PAUSED. "
            "Sin plan, el brief no es ejecutable. Presupuesto/plataforma deben coincidir; "
            "no inferir política, redes ni categorías. No crea grupos, anuncios ni segmentación."
        ),
    )

    @model_validator(mode="after")
    def validate_plan_matches_brief(self) -> ProposeCampaignArgs:
        if self.creation_plan is not None:
            plan = self.creation_plan.model_dump(mode="json")
            creation_budget(
                {
                    "creation_plan": plan,
                    "daily_budget_amount": self.daily_budget_amount,
                    "daily_budget_currency": _BUDGET_CURRENCY,
                }
            )
            if plan["platform"] != self.platform.value:
                raise CampaignCreationError("campaign_creation_scope_mismatch")
        return self


class ListOpportunitiesArgs(ToolArgs):
    business_id: BusinessIdStr


def build_opportunity_tool_definitions(
    services: OpportunityToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="propose_campaign",
            description=(
                "Propone una campana nueva (objetivo, presupuesto de prueba, "
                "duracion, criterio de exito/muerte): nace pendiente, "
                "nunca autonoma. Con varias cuentas/conexiones, indica account_ref exacto. "
                "Puedes aportar creation_plan explícito: sólo contenedor PAUSED, "
                "sin grupos/anuncios/segmentación. Si falta, no puede aprobarse para ejecutar."
            ),
            args_model=ProposeCampaignArgs,
            tool_class=ToolClass.PROPOSAL,
            handler=_propose_campaign(services.propose_campaign),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_opportunities",
            description="Propuestas de campana nueva todavia abiertas, por contribucion esperada.",
            args_model=ListOpportunitiesArgs,
            tool_class=ToolClass.READ,
            handler=_list_opportunities(services.list_opportunities),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _propose_campaign(
    use_case: ProposeCampaign,
) -> Handler[ProposeCampaignArgs, CampaignProposalOutcome]:
    async def handler(
        args: ProposeCampaignArgs, _caller_scope: CallerScope
    ) -> CampaignProposalOutcome:
        brief = CampaignBrief(
            objective=args.objective,
            platform=PlatformCode(args.platform.value),
            offering_id=args.offering_id,
            daily_budget=Money.of(args.daily_budget_amount, _BUDGET_CURRENCY),
            duration_days=args.duration_days,
            success_criterion=args.success_criterion,
            kill_criterion=args.kill_criterion,
            angle=args.angle,
            targeting_seed=args.targeting_seed,
            geo=args.geo,
            calendar_event_id=args.calendar_event_id,
            creation_plan=(
                None if args.creation_plan is None else args.creation_plan.model_dump(mode="json")
            ),
        )
        request = ProposeCampaignRequest(
            business_id=BusinessId.parse(args.business_id),
            brief=brief,
            account_ref=EntityRef.parse(args.account_ref) if args.account_ref else None,
        )
        try:
            return await use_case.execute(request)
        except OfferingNotFoundError as exc:
            raise EntityNotFoundError(f"offering_id desconocido: {exc}") from exc
        except NoActiveAccountForPlatformError as exc:
            raise EntityNotFoundError(f"sin cuenta activa en esa plataforma: {exc}") from exc
        except AmbiguousActiveAccountForPlatformError as exc:
            # Mas de una cuenta ACTIVE en esa plataforma: no hay forma de
            # elegir sin una entrada que desambigue (HANDOFF-ADS02
            # "Seleccion ambigua"). VALIDATION_ERROR, no ENTITY_NOT_FOUND --
            # la cuenta si existe, lo que falta es un identificador que
            # apunte a una sola.
            raise ToolValidationError(
                f"varias cuentas activas en esa plataforma, se requiere seleccion explicita: {exc}"
            ) from exc
        except DailyBudgetExceedsCapError as exc:
            raise GuardrailBlockedError(str(exc)) from exc
        except CampaignCreationError as exc:
            raise ToolValidationError(str(exc)) from exc

    return handler


def _list_opportunities(
    use_case: ListOpportunities,
) -> Handler[ListOpportunitiesArgs, tuple[OpenOpportunityView, ...]]:
    async def handler(
        args: ListOpportunitiesArgs, _caller_scope: CallerScope
    ) -> tuple[OpenOpportunityView, ...]:
        return await use_case.execute(business_id=BusinessId.parse(args.business_id))

    return handler
