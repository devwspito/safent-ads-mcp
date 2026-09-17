"""`AceStepMusicRenderer` implementa `MusicRendererPort` sobre
`acestep-api` (`infra/creative/music/README.md`: ACE-Step 1.5, MIT,
~8 s/pista en la DGX). El servicio "Not started yet" en este snapshot: sin
`base_url` configurada, `compose()` falla alto y claro con
`AceStepNotConfiguredError` en vez de intentar una llamada de red que
colgaria hasta el timeout de httpx."""

from __future__ import annotations

import httpx

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.render_specs import AudioAsset, MusicSpec
from safent_ads.shared.errors import InfrastructureError


class AceStepNotConfiguredError(InfrastructureError):
    """`ADS_ACESTEP_MUSIC_BASE_URL` no esta configurada: sin respaldo
    silencioso, el selector de renderizadores no puede elegir musica local."""


class AceStepRenderError(InfrastructureError):
    """`acestep-api` devolvio un error o audio vacio."""


class AceStepMusicRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str | None,
        asset_store: AssetStorePort,
        timeout_s: float = 60.0,
    ) -> None:
        self._http_client = http_client
        self._base_url = base_url.rstrip("/") if base_url else None
        self._asset_store = asset_store
        self._timeout_s = timeout_s

    async def compose(self, spec: MusicSpec) -> AudioAsset:
        if self._base_url is None:
            raise AceStepNotConfiguredError(
                "ADS_ACESTEP_MUSIC_BASE_URL no configurada; acestep-api no esta arrancado "
                "(infra/creative/music/README.md)"
            )
        response = await self._http_client.post(
            f"{self._base_url}/generate",
            json={
                "prompt": spec.mood_prompt,
                "duration_s": spec.duration_s,
                "genre_hint": spec.genre_hint,
            },
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        payload = response.content
        if not payload:
            raise AceStepRenderError("respuesta de /generate vacia")
        storage_uri = await self._asset_store.put(payload, MediaKind.AUDIO)
        return AudioAsset(
            storage_uri=storage_uri,
            duration_s=float(spec.duration_s),
            renderer_used=RendererName.ACE_STEP_1_5,
        )
