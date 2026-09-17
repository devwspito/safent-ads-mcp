"""`PolicyCheck`: reglas puras de norma de plataforma (spec.md FR-32,
creative-port.md §"Reglas invariables", threat-model.md C-30). Sin I/O: el
puerto async (`application/ports.py::PolicyCheckPort`) resuelve el activo y
llama a `evaluate_policy` con los datos ya en memoria.

Listas de terminos ilustrativas, no un catalogo legal exhaustivo de cada
plataforma — el objetivo es bloquear los patrones mas frecuentes antes de
proponer, no sustituir la revision humana."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.enums import Placement, PolicySeverity, PolicyVerdictResult
from safent_ads.shared.ids import PlatformCode

_MAX_UPPERCASE_RATIO = 0.3
_MIN_LEN_FOR_CAPS_CHECK = 12
_META_TEXT_COVERAGE_ADVISORY_PCT = 20.0
_PRIMARY_TEXT_WARN_LEN: dict[PlatformCode, int] = {
    PlatformCode.META: 125,
    PlatformCode.GOOGLE: 90,
}

_ABSOLUTE_FORBIDDEN_CLAIMS: frozenset[str] = frozenset(
    {"garantizado", "garantizada", "garantía de aprobado", "100% aprobados", "exito asegurado"}
)
_CONDITIONAL_FREE_CLAIM = "gratis"

_PERSONAL_ATTRIBUTE_TERMS: frozenset[str] = frozenset(
    {
        "si eres parado",
        "si estás desempleado",
        "desempleado como tú",
        "tu edad",
        "tu religión",
        "tu orientación sexual",
        "porque estás embarazada",
    }
)

_STYLE_TIC_PHRASES: frozenset[str] = frozenset(
    {
        "en el mundo actual",
        "sin duda alguna",
        "no dudes en",
        "descubre cómo",
        "descubre como",
        "revoluciona tu",
        "desbloquea tu potencial",
        "en un mundo cada vez más",
        "¿sabías que",
    }
)
_EXCESSIVE_PUNCTUATION_PATTERN = re.compile(r"[!¡]{2,}|—.*—.*—")


@dataclass(frozen=True, slots=True)
class PolicyFinding:
    code: str
    severity: PolicySeverity
    human_message: str


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    verdict: PolicyVerdictResult
    findings: Sequence[PolicyFinding]

    @property
    def is_publishable(self) -> bool:
        return self.verdict != PolicyVerdictResult.FAIL


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyCheckContext:
    """Entrada de `evaluate_policy`: todo lo que las reglas necesitan, ya
    resuelto por el adaptador (sin I/O dentro del dominio)."""

    platform: PlatformCode
    placement: Placement
    ad_copy: AdCopy
    is_genuinely_free: bool = False
    destination_url: str | None = None
    allowed_destination_domains: frozenset[str] = frozenset()
    text_coverage_pct: float | None = None
    exact_copy_rendered_by_model: bool = False


def _check_forbidden_claims(context: PolicyCheckContext) -> list[PolicyFinding]:
    text = f"{context.ad_copy.headline} {context.ad_copy.primary_text}".lower()
    findings: list[PolicyFinding] = []
    for claim in _ABSOLUTE_FORBIDDEN_CLAIMS:
        if claim in text:
            findings.append(
                PolicyFinding(
                    code="FORBIDDEN_CLAIM",
                    severity=PolicySeverity.FAIL,
                    human_message=f'Afirmación no permitida: "{claim}".',
                )
            )
    if _CONDITIONAL_FREE_CLAIM in text and not context.is_genuinely_free:
        findings.append(
            PolicyFinding(
                code="UNVERIFIED_FREE_CLAIM",
                severity=PolicySeverity.FAIL,
                human_message='"gratis" solo se permite si la oferta es realmente gratuita.',
            )
        )
    return findings


def _check_personal_attributes(context: PolicyCheckContext) -> list[PolicyFinding]:
    text = f"{context.ad_copy.headline} {context.ad_copy.primary_text}".lower()
    findings: list[PolicyFinding] = []
    for term in _PERSONAL_ATTRIBUTE_TERMS:
        if term in text:
            findings.append(
                PolicyFinding(
                    code="PERSONAL_ATTRIBUTE_INFERENCE",
                    severity=PolicySeverity.FAIL,
                    human_message="El texto infiere un atributo personal del destinatario.",
                )
            )
    return findings


def _check_style_tics(context: PolicyCheckContext) -> list[PolicyFinding]:
    text = f"{context.ad_copy.headline} {context.ad_copy.primary_text}".lower()
    findings: list[PolicyFinding] = []
    for phrase in _STYLE_TIC_PHRASES:
        if phrase in text:
            findings.append(
                PolicyFinding(
                    code="MODEL_STYLE_TIC",
                    severity=PolicySeverity.FAIL,
                    human_message="El texto suena a redacción de modelo, no a español natural.",
                )
            )
            break
    if _EXCESSIVE_PUNCTUATION_PATTERN.search(text):
        findings.append(
            PolicyFinding(
                code="MODEL_STYLE_TIC",
                severity=PolicySeverity.FAIL,
                human_message="Puntuación excesiva propia de redacción de modelo.",
            )
        )
    return findings


def _check_capitalisation(context: PolicyCheckContext) -> list[PolicyFinding]:
    headline = context.ad_copy.headline
    if len(headline) < _MIN_LEN_FOR_CAPS_CHECK:
        return []
    letters = [c for c in headline if c.isalpha()]
    if not letters:
        return []
    uppercase_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if uppercase_ratio > _MAX_UPPERCASE_RATIO:
        return [
            PolicyFinding(
                code="CAPS_ABUSE",
                severity=PolicySeverity.WARN,
                human_message="El titular usa mayúsculas en exceso.",
            )
        ]
    return []


def _check_length_limits(context: PolicyCheckContext) -> list[PolicyFinding]:
    max_len = _PRIMARY_TEXT_WARN_LEN.get(context.platform)
    if max_len is None or len(context.ad_copy.primary_text) <= max_len:
        return []
    return [
        PolicyFinding(
            code="PRIMARY_TEXT_TOO_LONG",
            severity=PolicySeverity.WARN,
            human_message=f"El texto principal supera los {max_len} caracteres recomendados.",
        )
    ]


def _check_text_coverage(context: PolicyCheckContext) -> list[PolicyFinding]:
    """Meta: cobertura de texto sobre imagen <=20% es solo advisory
    (creative-port.md, `plan.md §5` no lo marca como bloqueante)."""
    if context.platform != PlatformCode.META or context.placement != Placement.FEED:
        return []
    coverage = context.text_coverage_pct
    if coverage is None or coverage <= _META_TEXT_COVERAGE_ADVISORY_PCT:
        return []
    return [
        PolicyFinding(
            code="TEXT_COVERAGE_ADVISORY",
            severity=PolicySeverity.WARN,
            human_message="El texto cubre más del 20% de la imagen; Meta puede limitar el alcance.",
        )
    ]


def _check_destination_domain(context: PolicyCheckContext) -> list[PolicyFinding]:
    """threat-model.md C-30: destino allow-listed."""
    if context.destination_url is None:
        return []
    host = urlsplit(context.destination_url).netloc.lower()
    if host in context.allowed_destination_domains:
        return []
    return [
        PolicyFinding(
            code="DESTINATION_NOT_ALLOWLISTED",
            severity=PolicySeverity.FAIL,
            human_message=f"El destino {host!r} no está en la lista de dominios permitidos.",
        )
    ]


def _check_model_rendered_exact_copy(context: PolicyCheckContext) -> list[PolicyFinding]:
    """DGX smoke 2026-09-09 (`infra/creative/workflows/README.md`): el
    modelo desfigura texto pequeno de forma fiable ("Preara", "gradies").
    El copy exacto siempre se compone con `BannerComposerPort`, nunca se
    confia al render; `exact_copy_rendered_by_model=True` marca que ese
    contrato se rompio en algun punto de la generacion."""
    if not context.exact_copy_rendered_by_model:
        return []
    return [
        PolicyFinding(
            code="MODEL_RENDERED_EXACT_COPY",
            severity=PolicySeverity.FAIL,
            human_message=(
                "El copy exacto no puede depender del texto que dibuja el modelo; "
                "debe componerse con BannerComposer."
            ),
        )
    ]


_RULES = (
    _check_forbidden_claims,
    _check_personal_attributes,
    _check_style_tics,
    _check_capitalisation,
    _check_length_limits,
    _check_text_coverage,
    _check_destination_domain,
    _check_model_rendered_exact_copy,
)


def _combine_verdict(findings: Sequence[PolicyFinding]) -> PolicyVerdictResult:
    if any(f.severity == PolicySeverity.FAIL for f in findings):
        return PolicyVerdictResult.FAIL
    if any(f.severity == PolicySeverity.WARN for f in findings):
        return PolicyVerdictResult.WARN
    return PolicyVerdictResult.PASS_


def evaluate_policy(context: PolicyCheckContext) -> PolicyVerdict:
    findings: list[PolicyFinding] = []
    for rule in _RULES:
        findings.extend(rule(context))
    return PolicyVerdict(verdict=_combine_verdict(findings), findings=tuple(findings))
