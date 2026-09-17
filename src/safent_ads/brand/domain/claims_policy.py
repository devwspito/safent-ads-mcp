"""Politica de reclamos: allow-list por negocio mas un **suelo de
seguridad** que ningun kit puede rebajar -- reclamos de resultado que
ningun negocio puede prometer, validos para cualquier vertical. Los
reclamos propios de un sector concreto (p.ej. "curacion garantizada" en
salud) no van aqui: los anade el propietario en su YAML
(`forbidden_claims`), que `normalize_forbidden_claims` une con este suelo.
El suelo es un valor de producto, no un dato de un negocio concreto: vive
aqui, no en el YAML que rellena el propietario."""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_FORBIDDEN_CLAIMS: frozenset[str] = frozenset(
    {
        "garantizado",
        "garantizada",
        "exito asegurado",
    }
)


def normalize_forbidden_claims(claims: Iterable[str]) -> frozenset[str]:
    """Union con el suelo de seguridad (nunca una resta): quien construye
    un `BrandKit` (repositorio, cargador YAML) llama a esto antes de
    pasarle `forbidden_claims` al agregado, que a su vez valida que el
    suelo siga presente (`BrandKit.__post_init__`)."""
    return frozenset(claim.strip().casefold() for claim in claims if claim.strip()) | {
        c.casefold() for c in DEFAULT_FORBIDDEN_CLAIMS
    }


def contains_forbidden_claim(text: str, forbidden_claims: Iterable[str]) -> bool:
    """Coincidencia por subcadena, insensible a mayusculas: suficiente para
    el suelo de seguridad y para lo que el propietario anada; el matching
    linguistico fino (variantes, sinonimos) es trabajo de
    `check_copy_compliance` (tool-surface.md §2.6, fuera de este lane)."""
    normalized_text = text.casefold()
    return any(claim.casefold() in normalized_text for claim in forbidden_claims)


def normalize_claims_allowlist(claims: Iterable[str]) -> frozenset[str]:
    """De-dup insensible a mayusculas de `claims_allowlist`, conservando la
    primera grafia recibida para cada reclamo (a diferencia del suelo de
    reclamos prohibidos, aqui no forzamos minusculas: es texto que el
    panel muestra tal cual lo escribio el propietario)."""
    first_seen_casing: dict[str, str] = {}
    for claim in claims:
        stripped = claim.strip()
        if not stripped:
            continue
        first_seen_casing.setdefault(stripped.casefold(), stripped)
    return frozenset(first_seen_casing.values())


def find_allow_forbid_conflicts(
    claims_allowlist: Iterable[str], forbidden_claims: Iterable[str]
) -> frozenset[str]:
    """Reclamos permitidos que coinciden EXACTAMENTE (insensible a
    mayusculas, tras recortar espacios) con uno prohibido -- incluido el
    suelo. Coincidencia exacta, no por subcadena
    (`contains_forbidden_claim` es para escanear una frase de copy larga
    contra la lista; aqui comparamos dos frases de reclamo cortas entre
    si, y una coincidencia parcial daria falsos positivos ruidosos)."""
    forbidden_cf = {claim.strip().casefold() for claim in forbidden_claims}
    return frozenset(
        claim for claim in claims_allowlist if claim.strip().casefold() in forbidden_cf
    )
