"""`PlatformAdapterRegistry`: enruta por `PlatformCode` hacia el adaptador
concreto (`GoogleAdsAdapter`/`MetaAdsAdapter`). Unico punto donde el
`socket_server` decide "que SDK habla con esta cuenta" (plan.md §5:
"En `broker/platforms/`... unica ubicacion con SDKs")."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import PlatformCode


class UnknownPlatformError(InfrastructureError):
    """No hay adaptador registrado para la plataforma pedida."""


@dataclass(frozen=True, slots=True)
class PlatformAdapterRegistry:
    adapters: Mapping[PlatformCode, AdsPlatformPort]

    def get(self, platform: PlatformCode) -> AdsPlatformPort:
        adapter = self.adapters.get(platform)
        if adapter is None:
            raise UnknownPlatformError(str(platform))
        return adapter
