"""`UpdateBrandClaims`: caso de uso que respalda `PUT /brand/claims`
(rest-api.md §Marca) -- el unico camino para que el propietario edite
`claims_allowlist`, `forbidden_claims` (anadidos suyos sobre el suelo) y
`legal_disclaimers` sin pasar por `POST /brand/confirm`, que a proposito
no gestiona estos tres campos (`ConfirmBrandDraft`). Funciona igual sobre
un kit confirmado que sobre una vista previa de borrador
(`is_confirmed: false`): la politica de reclamos no depende de si el
propietario ya confirmo tipografia/paleta/tono."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.ports import BrandClaimsDecisionRecorder, BrandKitRepository
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True)
class UpdateBrandClaimsRequest:
    business_id: BusinessId
    actor_email: str
    claims_allowlist: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    legal_disclaimers: tuple[LegalDisclaimer, ...] = ()


class UpdateBrandClaims:
    def __init__(
        self,
        *,
        brand_kits: BrandKitRepository,
        clock: Clock,
        decision_recorder: BrandClaimsDecisionRecorder,
    ) -> None:
        self._brand_kits = brand_kits
        self._clock = clock
        self._decision_recorder = decision_recorder

    async def execute(self, request: UpdateBrandClaimsRequest) -> BrandKit:
        kit = await self._brand_kits.get_by_business(request.business_id)
        if kit is None:
            raise BrandKitNotFoundError(
                f"sin kit de marca para el negocio {request.business_id}"
            )
        updated = kit.replace_claims(
            claims_allowlist=request.claims_allowlist,
            forbidden_claims=request.forbidden_claims,
            legal_disclaimers=request.legal_disclaimers,
            updated_at=self._clock.now(),
        )
        await self._brand_kits.save(updated)
        await self._decision_recorder.record(
            business_id=request.business_id, actor_email=request.actor_email
        )
        return updated
