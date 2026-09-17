"""`CreativeUploadPort` (004 tasks-2.md W4, historia 13): puerto fino hacia
`creative`, mismo criterio que `proposal_write_port.py` -- un unico llamador
(`upload_creative_asset`), asi que el puerto es casi un alias de su firma.
La implementacion real (`composition/creative_upload_adapter.py`) traduce
las excepciones de `creative.application`/`creative.infrastructure` a los
errores tipados de `mcp.application.errors`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CreativeUploadResult:
    asset_id: str
    media_kind: str
    preview_url: str | None


class CreativeUploadPort(Protocol):
    async def upload_creative_asset(
        self,
        *,
        business_id: str,
        brief_id: str,
        media_kind: str,
        content: bytes,
        native_tool_used: str,
    ) -> CreativeUploadResult: ...
