"""`ChatterboxVoiceRenderer` implementa `VoiceRendererPort` sobre el
servidor Chatterbox-TTS de `infra/creative/tts/` (Chatterbox Multilingual
v3, MIT, es-ES; research/content-generation-stack.md §4). HTTP simple:
`POST /tts` con el texto y el idioma, cuerpo de respuesta = audio (WAV,
`config.yaml audio_output.format: wav`).

`infra/creative/tts/compose.yaml` marca el servidor "NOT STARTED YET": la
forma exacta del endpoint (`devnen/Chatterbox-TTS-Server` corriente arriba)
no se ha verificado contra un servidor real todavia — este adaptador sigue
la convencion REST mas comun del proyecto y se prueba contra un HTTP falso;
ajustar la ruta/cuerpo si el servidor real difiere al levantarlo."""

from __future__ import annotations

import httpx

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.render_specs import AudioAsset, VoiceSpec
from safent_ads.shared.errors import InfrastructureError


class ChatterboxTtsError(InfrastructureError):
    """El servidor Chatterbox-TTS devolvio un error o audio vacio."""


class ChatterboxVoiceRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        asset_store: AssetStorePort,
        timeout_s: float = 60.0,
    ) -> None:
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._asset_store = asset_store
        self._timeout_s = timeout_s

    async def synthesize(self, spec: VoiceSpec) -> AudioAsset:
        response = await self._http_client.post(
            f"{self._base_url}/tts",
            json={
                "text": spec.text,
                "language": spec.language.value,
                "voice_id": spec.voice_id,
                "speed": spec.speed,
            },
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        payload = response.content
        if not payload:
            raise ChatterboxTtsError("respuesta de /tts vacia")
        duration_s = float(response.headers.get("x-audio-duration-s", 0.0)) or _estimate_duration(
            spec.text, spec.speed
        )
        storage_uri = await self._asset_store.put(payload, MediaKind.AUDIO)
        return AudioAsset(
            storage_uri=storage_uri,
            duration_s=duration_s,
            renderer_used=RendererName.CHATTERBOX_ES_ES,
        )


_WORDS_PER_SECOND_ES = 2.5


def _estimate_duration(text: str, speed: float) -> float:
    """Reserva si el servidor no manda `X-Audio-Duration-S`: ritmo de habla
    medio en espanol (~150 palabras/min), ajustado por `speed`."""
    word_count = max(1, len(text.split()))
    return round(word_count / (_WORDS_PER_SECOND_ES * speed), 2)
