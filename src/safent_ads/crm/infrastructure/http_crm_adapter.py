"""`HttpCrmAdapter` (plan.md §5, tasks.md T032): implementa `CrmPort` sobre
un endpoint HTTP que devuelve conversiones en JSON. Unico adaptador de
`crm` que habla HTTP (oposads-assessment: 'Adapter is CRM-specific; keep
behind CrmPort').

`base_url`, `token` y el `field_mapping` declarativo (`conversion_mapper.
ConversionFieldMapping`) se inyectan por constructor, no se leen de
`composition.settings` desde aqui — el cableado real (lectura de settings,
credenciales) lo hace `composition` cuando exista soporte para ellas.
Cualquier CRM que exponga sus conversiones como JSON encaja aqui sin
tocar el adaptador: solo cambian `base_url`, `token` y `field_mapping`."""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from safent_ads.crm.application.ports import CrmConversionsRequest, CrmPort
from safent_ads.crm.domain.attribution_resolver import ConversionSignal
from safent_ads.crm.infrastructure.conversion_mapper import (
    DEFAULT_CONVERSION_FIELD_MAPPING,
    ConversionFieldMapping,
    parse_conversion_signal,
)
from safent_ads.crm.infrastructure.errors import CrmRequestError

_REQUEST_TIMEOUT_SECONDS = 10.0
_CONVERSIONS_PATH = "/api/conversions"


class HttpCrmAdapter(CrmPort):
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        field_mapping: ConversionFieldMapping = DEFAULT_CONVERSION_FIELD_MAPPING,
    ) -> None:
        self._token = token
        self._field_mapping = field_mapping
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def fetch_conversions(
        self, request: CrmConversionsRequest
    ) -> Sequence[ConversionSignal]:
        response = await self._get_conversions(request)
        return [
            parse_conversion_signal(request.business_id, item, self._field_mapping)
            for item in response
        ]

    async def _get_conversions(self, request: CrmConversionsRequest) -> list[dict[str, object]]:
        try:
            response = await self._client.get(
                _CONVERSIONS_PATH,
                headers={"Authorization": f"Bearer {self._token}"},
                params={
                    "business_id": str(request.business_id),
                    "window_start": request.window_start.isoformat(),
                    "window_end": request.window_end.isoformat(),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CrmRequestError("fallo al consultar el CRM") from exc
        return list(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()
