"""Traduce agregados de `creative` a la forma de `contracts/rest-api.md`
§Creatividades / panel `api/schemas/creatives.ts` (zod), consumida por
`router.py`. `mcp_tools.py` (superficie de `contracts/mcp-tools.md`, un
contrato distinto y mas simple, todavia sin cablear a ningun
`ToolRegistry`) mantiene sus propias funciones de traduccion locales --
las dos superficies dejaron de compartir forma en cuanto el panel necesito
`label`/`spend`/`signal`/etc, que `contracts/mcp-tools.md` no pide.

Funciones puras: nunca hacen I/O. `label`/`preview_url` los resuelve el
router ANTES de llamar aqui (brief_id -> `CreativeBrief.hook`,
`AssetStorePort.signed_preview_url`) -- ninguno de los dos vive en el
propio `CreativeAsset`.

**Zod vs dominio (panel `api/schemas/creatives.ts`, seguido a proposito
donde difiere del dominio -- ver informe de esta rama):**
- `signal` (zod: `FATIGUE|WINNER|LOSER|LEARNING`) vs `CreativeOutcome`
  (dominio: `pending|winner|loser|fatigue`, sin `learning`): `PENDING`
  se proyecta a `LEARNING` (mismo significado -- "todavia sin resultado
  concluyente"), unica correspondencia razonable sin un quinto valor.
- `policy_verdict` (zod: `PASS|FAIL|PENDING`) vs `PolicyVerdictResult`
  (dominio: `PASS|WARN|FAIL`, mas la ausencia de veredicto = `None`):
  `None` -> `PENDING` (no revisado todavia); `WARN` -> `FAIL` (mas
  conservador: `propose-publication` ya bloquea `WARN` con
  `POLICY_CHECK_REQUIRED`, la insignia de la lista no puede decir "verde"
  para algo que la propia puerta trata como no publicable).
- `spend`/`hook_rate_pct`/`hold_rate_pct`/`frequency`/`ads_running_on`:
  metricas de `execution`/`economics` (fuera de este bounded context,
  plan.md §4 -- `creative` no las importa). Cero/`null`/`[]` HONESTOS
  (ningun activo de esta rama ha sido publicado nunca, FR-33: `creative`
  jamas publica de forma autonoma), nunca un valor inventado."""

from __future__ import annotations

from datetime import datetime

from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.creative_job import CreativeJob
from safent_ads.creative.domain.enums import CreativeOutcome, PolicyVerdictResult
from safent_ads.creative.domain.identifiers import AssetId, JobId
from safent_ads.creative.domain.policy import PolicyVerdict

_JOB_PROGRESS_SCALE_TO_PERCENT = 100

_SIGNAL_BY_OUTCOME: dict[CreativeOutcome, str] = {
    CreativeOutcome.PENDING: "LEARNING",
    CreativeOutcome.WINNER: "WINNER",
    CreativeOutcome.LOSER: "LOSER",
    CreativeOutcome.FATIGUE: "FATIGUE",
}

_ZOD_POLICY_VERDICT_BY_RESULT: dict[PolicyVerdictResult, str] = {
    PolicyVerdictResult.PASS_: "PASS",
    PolicyVerdictResult.WARN: "FAIL",
    PolicyVerdictResult.FAIL: "FAIL",
}
_POLICY_VERDICT_PENDING = "PENDING"


def outcome_to_zod_signal(outcome: CreativeOutcome) -> str:
    return _SIGNAL_BY_OUTCOME[outcome]


def policy_verdict_to_zod(verdict: PolicyVerdict | None) -> str:
    if verdict is None:
        return _POLICY_VERDICT_PENDING
    return _ZOD_POLICY_VERDICT_BY_RESULT[verdict.verdict]


def policy_findings_to_zod(verdict: PolicyVerdict | None) -> list[str]:
    if verdict is None:
        return []
    return [finding.human_message for finding in verdict.findings]


def asset_json(
    asset_id: AssetId,
    asset: CreativeAsset,
    *,
    label: str,
    preview_url: str,
    now: datetime,
) -> dict[str, object]:
    """Forma completa de `creativeAssetSchema` (panel), usada tanto en
    `GET /creatives`/`GET /creatives/{asset_id}` como en los `assets`
    embebidos de `GET /creative-jobs/{job_id}`."""
    return {
        "asset_id": str(asset_id),
        "business_id": str(asset.business_id),
        "label": label,
        "media_kind": asset.media_kind.value,
        # `format` puede ser `None` en un activo importado (`import_creative_asset`)
        # todavia sin cotejar contra ningun preset de anuncio -- zod exige
        # el campo no-nulo; discrepancia documentada en el docstring del
        # modulo, no se rellena con un valor inventado.
        "format": asset.format.value if asset.format is not None else None,
        "preview_url": preview_url,
        "signal": outcome_to_zod_signal(asset.outcome),
        "policy_verdict": policy_verdict_to_zod(asset.policy_verdict),
        "policy_findings": policy_findings_to_zod(asset.policy_verdict),
        "review_state": asset.review_state.value,
        "spend": {"amount": 0.0, "currency": asset.cost_estimate.currency},
        "hook_rate_pct": None,
        "hold_rate_pct": None,
        "frequency": None,
        "days_in_rotation": _days_in_rotation(asset, now),
        "ads_running_on": [],
        "signal_id": (
            str(asset.provenance.source_signal_id) if asset.provenance.source_signal_id else None
        ),
        "brief_id": str(asset.provenance.brief_id),
    }


def job_json(
    job_id: JobId,
    job: CreativeJob,
    asset_rows: list[tuple[CreativeAsset, str, str]],
    *,
    now: datetime,
) -> dict[str, object]:
    """`creativeJobSchema`: `state` en MAYUSCULAS y `progress` en 0-100 --
    el dominio usa minusculas y fraccion 0-1 (`CreativeJobState.value`,
    `CreativeJob.progress`); se traduce aqui, unico punto de cruce a JSON.
    `asset_rows`: `(asset, label, preview_url)` ya resueltos por el router,
    en el mismo orden que `job.asset_ids`."""
    first_asset = asset_rows[0][0] if asset_rows else None
    return {
        "job_id": str(job_id),
        "state": job.state.value.upper(),
        "progress": job.progress * _JOB_PROGRESS_SCALE_TO_PERCENT,
        "assets": [
            asset_json(asset.asset_id, asset, label=label, preview_url=preview_url, now=now)
            for asset, label, preview_url in asset_rows
        ],
        "renderer_used": (
            first_asset.provenance.renderer_used.value if first_asset is not None else None
        ),
        "cost_estimate": (
            {
                "amount": float(first_asset.cost_estimate.amount),
                "currency": first_asset.cost_estimate.currency,
            }
            if first_asset is not None
            else None
        ),
    }


def _days_in_rotation(asset: CreativeAsset, now: datetime) -> int:
    elapsed = now - asset.provenance.generated_at
    return max(elapsed.days, 0)
