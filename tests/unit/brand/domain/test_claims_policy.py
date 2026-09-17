"""`claims_policy`: union con el suelo de seguridad, nunca una resta."""

from __future__ import annotations

from safent_ads.brand.domain.claims_policy import (
    DEFAULT_FORBIDDEN_CLAIMS,
    contains_forbidden_claim,
    find_allow_forbid_conflicts,
    normalize_claims_allowlist,
    normalize_forbidden_claims,
)


def test_normalize_always_includes_the_baseline() -> None:
    normalized = normalize_forbidden_claims(())

    assert {c.casefold() for c in DEFAULT_FORBIDDEN_CLAIMS} <= normalized


def test_normalize_adds_owner_claims_on_top_of_baseline() -> None:
    normalized = normalize_forbidden_claims(["nota media mas alta de españa"])

    assert "nota media mas alta de españa" in normalized
    assert {c.casefold() for c in DEFAULT_FORBIDDEN_CLAIMS} <= normalized


def test_normalize_strips_blank_and_lowercases() -> None:
    normalized = normalize_forbidden_claims(["  GARANTIZADO  ", ""])

    assert "garantizado" in normalized


def test_contains_forbidden_claim_matches_substring_case_insensitive() -> None:
    assert contains_forbidden_claim("Plaza ASEGURADA para todos", {"plaza asegurada"})


def test_contains_forbidden_claim_false_when_absent() -> None:
    assert not contains_forbidden_claim("Clases en directo", {"plaza asegurada"})


def test_normalize_claims_allowlist_strips_and_drops_blanks() -> None:
    normalized = normalize_claims_allowlist(["  Envio gratis  ", "", "   "])

    assert normalized == frozenset({"Envio gratis"})


def test_normalize_claims_allowlist_deduplicates_case_insensitively_keeping_first_casing() -> None:
    normalized = normalize_claims_allowlist(["Envio Gratis", "envio gratis", "ENVIO GRATIS"])

    assert normalized == frozenset({"Envio Gratis"})


def test_find_allow_forbid_conflicts_matches_exactly_case_insensitive() -> None:
    conflicts = find_allow_forbid_conflicts(
        claims_allowlist=["Mejor del mercado", "Envio gratis"],
        forbidden_claims=["mejor del mercado"],
    )

    assert conflicts == frozenset({"Mejor del mercado"})


def test_find_allow_forbid_conflicts_does_not_match_by_substring() -> None:
    """Distinto de `contains_forbidden_claim`: dos frases de reclamo se
    comparan enteras, no una como subcadena de la otra."""
    conflicts = find_allow_forbid_conflicts(
        claims_allowlist=["Mejor calidad del mercado local"],
        forbidden_claims=["mejor del mercado"],
    )

    assert conflicts == frozenset()


def test_find_allow_forbid_conflicts_empty_when_no_overlap() -> None:
    conflicts = find_allow_forbid_conflicts(
        claims_allowlist=["Envio gratis"], forbidden_claims=["garantizado"]
    )

    assert conflicts == frozenset()
