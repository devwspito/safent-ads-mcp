"""`build_tracking_template`/`find_utm_inconsistency` (T159): plantilla UTM
determinista por plataforma/campana y el validador que reporta divergencias
entre lo observado y esa plantilla. `test_spend_increase_blocked_without_
valid_tracking` (T158/T159 cruzados): UTM roto es una de las senales del
nodo 1 de diagnostico (`optimization.domain.diagnosis.is_measurement_
broken`) -- congelar BUY, no una coincidencia de nombres."""

from __future__ import annotations

from safent_ads.economics.domain.tracking import (
    UtmIssue,
    build_tracking_template,
    find_utm_inconsistency,
)
from safent_ads.optimization.domain.diagnosis import is_measurement_broken


def test_build_tracking_template_sets_the_fixed_utm_fields() -> None:
    template = build_tracking_template(platform="google", campaign_ref="Busqueda Marca")

    assert template.utm["utm_source"] == "google"
    assert template.utm["utm_medium"] == "cpc"
    assert template.utm["utm_campaign"] == "busqueda-marca"
    assert "gclid={gclid}" in template.final_url_suffix


def test_build_tracking_template_includes_optional_offering_and_event() -> None:
    template = build_tracking_template(
        platform="meta",
        campaign_ref="Retargeting",
        offering_code="MAT-2026",
        calendar_event_code="Evento Marzo",
    )

    assert template.utm["utm_content"] == "mat-2026"
    assert template.utm["utm_term"] == "evento-marzo"
    assert "fbclid={fbclid}" in template.final_url_suffix


def test_build_tracking_template_without_optional_fields_omits_them() -> None:
    template = build_tracking_template(platform="google", campaign_ref="c1")

    assert "utm_content" not in template.utm
    assert "utm_term" not in template.utm


def test_missing_tracking_data_is_its_own_issue() -> None:
    template = build_tracking_template(platform="google", campaign_ref="c1")

    issue = find_utm_inconsistency(template=template, observed_final_url_suffix=None)

    assert issue is UtmIssue.NO_TRACKING_DATA


def test_blank_tracking_data_is_also_no_tracking_data() -> None:
    template = build_tracking_template(platform="google", campaign_ref="c1")

    issue = find_utm_inconsistency(template=template, observed_final_url_suffix="   ")

    assert issue is UtmIssue.NO_TRACKING_DATA


def test_a_missing_utm_param_is_reported() -> None:
    template = build_tracking_template(platform="google", campaign_ref="c1")

    issue = find_utm_inconsistency(
        template=template, observed_final_url_suffix="utm_source=google&utm_medium=cpc"
    )

    assert issue is UtmIssue.MISSING_PARAM


def test_a_mismatched_utm_value_is_reported() -> None:
    template = build_tracking_template(platform="google", campaign_ref="c1")

    issue = find_utm_inconsistency(
        template=template,
        observed_final_url_suffix="utm_source=google&utm_medium=cpc&utm_campaign=otra-cosa",
    )

    assert issue is UtmIssue.VALUE_MISMATCH


def test_matching_utm_params_are_consistent() -> None:
    template = build_tracking_template(platform="google", campaign_ref="c1")

    issue = find_utm_inconsistency(
        template=template,
        observed_final_url_suffix="utm_source=google&utm_medium=cpc&utm_campaign=c1"
        "&gclid=abc123",
    )

    assert issue is None


def test_spend_increase_blocked_without_valid_tracking() -> None:
    """Sin UTM valido, el nodo 1 (medicion) del arbol de diagnostico
    dispara -- profitability-engine.md §5: 'Congelar BUY + arreglo' es la
    accion, no una subida de gasto."""
    template = build_tracking_template(platform="google", campaign_ref="c1")
    issue = find_utm_inconsistency(template=template, observed_final_url_suffix=None)

    broken = is_measurement_broken(
        unattributed_share=0.0,
        delta_hat=1.0,
        utm_valid=issue is None,
        bridge_has_recent_events_24h=True,
    )

    assert broken is True
