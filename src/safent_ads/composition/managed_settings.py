"""Server-only managed central profile; never a caller-selected edition."""

from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings

from safent_ads.iam.infrastructure.enterprise_ads_authority import EnterpriseAdsTrust
from safent_ads.iam.infrastructure.enterprise_seat_authority import EnterpriseSeatTrust


class ManagedAdsSettings(BaseSettings):
    managed_central: bool = Field(default=False, validation_alias="ADS_MANAGED_CENTRAL")
    # 004 tasks.md A10: puestos de anuncios por persona (`/mcp` enrutado por
    # permiso). Comparte el mismo trio `enterprise_*` que `managed_central`
    # -- ambos hablan con el mismo Enterprise, con secretos de servicio
    # potencialmente distintos por despliegue pero la misma forma de trust.
    seat_authority_enabled: bool = Field(
        default=False, validation_alias="ADS_SEAT_AUTHORITY_ENABLED"
    )
    # Modo de un solo propietario (Safent local/companion, motor Hermes):
    # modo de primera clase, no un flag de desarrollo. `ApiSettings` exige
    # exactamente uno de `seat_authority_enabled`/`single_owner_mode`
    # (fuera de `managed_central`) -- ver su propio validador.
    single_owner_mode: bool = Field(default=False, validation_alias="ADS_SINGLE_OWNER_MODE")
    enterprise_origin: str = Field(default="", validation_alias="ADS_ENTERPRISE_ORIGIN")
    enterprise_service_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias="ADS_ENTERPRISE_SERVICE_SECRET"
    )
    enterprise_org_ids: frozenset[UUID] = Field(
        default_factory=frozenset, validation_alias="ADS_ENTERPRISE_ORG_IDS"
    )

    @model_validator(mode="after")
    def validate_managed_trust(self) -> "ManagedAdsSettings":
        if self.managed_central:
            self.managed_trust()
        if self.seat_authority_enabled:
            self.seat_trust()
        return self

    def managed_trust(self) -> EnterpriseAdsTrust:
        if not self.managed_central:
            raise ValueError("managed central is not enabled")
        return EnterpriseAdsTrust(
            self.enterprise_origin,
            self.enterprise_service_secret.get_secret_value(),
            self.enterprise_org_ids,
        )

    def seat_trust(self) -> EnterpriseSeatTrust:
        if not self.seat_authority_enabled:
            raise ValueError("seat authority is not enabled")
        return EnterpriseSeatTrust(
            self.enterprise_origin,
            self.enterprise_service_secret.get_secret_value(),
            self.enterprise_org_ids,
        )
