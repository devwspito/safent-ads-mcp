"""Explicit catalog write under the ordinary MCP jail, never platform execution."""

from pydantic import ConfigDict, model_validator

from safent_ads.catalog.application.create_offering import (
    CreatedOffering,
    CreateOffering,
    OfferingBusinessNotFoundError,
    OfferingCodeConflictError,
)
from safent_ads.catalog.domain.offering import OfferingDetails
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.errors import EntityNotFoundError, ToolValidationError
from safent_ads.mcp.presentation.args import BusinessId as BusinessIdStr
from safent_ads.mcp.presentation.args import ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.shared.ids import BusinessId


class CreateOfferingArgs(ToolArgs):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, str_strip_whitespace=False)
    business_id: BusinessIdStr
    code: str
    title: str
    price_amount: str | None = None
    price_currency: str | None = None

    def details(self) -> OfferingDetails:
        return OfferingDetails(self.code, self.title, self.price_amount, self.price_currency)

    @model_validator(mode="after")
    def validate_details(self) -> "CreateOfferingArgs":
        self.details()
        return self


def build_offering_tool(service: CreateOffering) -> ToolDefinition[CreateOfferingArgs]:
    async def create(args: CreateOfferingArgs, _caller: CallerScope) -> CreatedOffering:
        try:
            return await service.execute(BusinessId.parse(args.business_id), args.details())
        except OfferingBusinessNotFoundError as exc:
            raise EntityNotFoundError("Negocio no disponible.") from exc
        except OfferingCodeConflictError as exc:
            raise ToolValidationError(
                "OFFERING_CODE_CONFLICT: revisa la oferta existente o elige otro código."
            ) from exc

    return ToolDefinition(
        name="create_offering",
        description=(
            "Da de alta una oferta/producto local con código y título proporcionados por el dueño. "
            "Precio y moneda opcionales, siempre ambos o ninguno; no inventes importes. "
            "Escritura de catálogo sujeta a aprobación de la jaula; "
            "no crea campañas ni cambia presupuestos. "
            "Repetir el mismo código/datos es idempotente; nunca sobrescribe otra oferta."
        ),
        args_model=CreateOfferingArgs,
        tool_class=ToolClass.CATALOG_WRITE,
        handler=create,
        business_id_of=lambda args: args.business_id,
    )
