"""`build_tracking_template`/`find_utm_inconsistency` (T159, tool-surface.md
§2.5: "UTMs y plantilla de seguimiento coherentes; sin esto la atribucion no
cierra" / "detecta anuncios con UTM roto o divergente"). Puro: sin I/O, sin
`Clock` -- las dos herramientas MCP de la misma tarea solo envuelven esto.

profitability-engine.md §9: "build_tracking_template y validate_utm_
consistency pasan a bloqueantes: sin medicion integra no se sube gasto"."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import parse_qsl

_CLICK_ID_PARAM_BY_PLATFORM: dict[str, str] = {"google": "gclid", "meta": "fbclid"}
_SLUG_FORBIDDEN = " \t\n"


class UtmIssue(StrEnum):
    NO_TRACKING_DATA = "no_tracking_data"
    MISSING_PARAM = "missing_param"
    VALUE_MISMATCH = "value_mismatch"


@dataclass(frozen=True, kw_only=True, slots=True)
class TrackingTemplate:
    """`{final_url_suffix, utm}` (contracts/mcp-tools.md `build_tracking_
    template`). `utm` es la lista blanca exacta que `find_utm_inconsistency`
    compara contra lo observado -- ningun parametro fuera de esta lista se
    valida ni se exige."""

    final_url_suffix: str
    utm: MappingProxyType[str, str]


def build_tracking_template(
    *,
    platform: str,
    campaign_ref: str,
    offering_code: str | None = None,
    calendar_event_code: str | None = None,
) -> TrackingTemplate:
    """`utm_source`/`utm_medium` fijos por plataforma (siempre CPC: esta
    superficie no cubre organico/email); `utm_campaign` identifica la
    campana; `utm_content`/`utm_term` opcionales anclan el producto y el
    evento de calendario cuando se conocen (calendar_event_id -> offering,
    mismo enlace que `economics.application.cohort_builder`). El id de
    clic de la plataforma viaja como placeholder ValueTrack/URL param -- la
    plataforma lo sustituye en el clic real, nunca este generador."""
    utm: dict[str, str] = {
        "utm_source": platform,
        "utm_medium": "cpc",
        "utm_campaign": _slug(campaign_ref),
    }
    if offering_code:
        utm["utm_content"] = _slug(offering_code)
    if calendar_event_code:
        utm["utm_term"] = _slug(calendar_event_code)
    query = "&".join(f"{key}={value}" for key, value in utm.items())
    click_id_param = _CLICK_ID_PARAM_BY_PLATFORM.get(platform)
    if click_id_param:
        query = f"{query}&{click_id_param}={{{click_id_param}}}"
    return TrackingTemplate(final_url_suffix=query, utm=MappingProxyType(utm))


def find_utm_inconsistency(
    *, template: TrackingTemplate, observed_final_url_suffix: str | None
) -> UtmIssue | None:
    """`None` = coherente. Sin dato observado (ningun `final_url_suffix`
    conocido para el anuncio) se reporta `NO_TRACKING_DATA`, no se asume
    que esta bien -- 'no hay numero' (profitability-engine.md §7) aplica
    igual aqui: la ausencia de medicion nunca se disfraza de medicion
    conforme."""
    if observed_final_url_suffix is None or not observed_final_url_suffix.strip():
        return UtmIssue.NO_TRACKING_DATA
    observed = dict(parse_qsl(observed_final_url_suffix))
    for key, expected_value in template.utm.items():
        if key not in observed:
            return UtmIssue.MISSING_PARAM
        if observed[key] != expected_value:
            return UtmIssue.VALUE_MISMATCH
    return None


def _slug(value: str) -> str:
    normalized = value.strip().lower()
    for character in _SLUG_FORBIDDEN:
        normalized = normalized.replace(character, "-")
    return normalized
