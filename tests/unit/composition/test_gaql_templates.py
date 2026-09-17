"""`load_gaql_template` (T162): cada plantilla (package data `broker/platforms/gaql/*.gaql`) (a)
supera `validate_gaql` (el mismo validador de `run_gaql`) y (b) reproduce
caracter a caracter la consulta que hoy compone `google_ads_adapter.py` a
partir de sus constantes -- la prueba de que mover la consulta a fichero
"mantiene el comportamiento identico" sin necesitar tocar `broker/` (otro
carril) para demostrarlo."""

from __future__ import annotations

import importlib.resources

import pytest

from safent_ads.broker.platforms.google_ads_adapter import (
    _AD_FIELDS,
    _AD_GROUP_FIELDS,
    _CAMPAIGN_FIELDS,
    _METRIC_FIELDS,
    _build_select,
)
from safent_ads.composition.gaql_templates import (
    GAQL_TEMPLATE_NAMES,
    GaqlTemplateError,
    load_gaql_template,
)


@pytest.mark.parametrize("name", GAQL_TEMPLATE_NAMES)
def test_every_template_parses_and_validates(name: str) -> None:
    query = load_gaql_template(name)

    assert query.strip().upper().startswith("SELECT")
    assert "FROM" in query.upper()


def test_missing_template_fails_loudly() -> None:
    with pytest.raises(GaqlTemplateError):
        load_gaql_template("does_not_exist")


def test_campaign_inventory_matches_todays_inline_query() -> None:
    assert load_gaql_template("campaign_inventory") == _build_select(_CAMPAIGN_FIELDS, "campaign")


def test_ad_group_inventory_matches_todays_inline_query() -> None:
    assert load_gaql_template("ad_group_inventory") == _build_select(_AD_GROUP_FIELDS, "ad_group")


def test_ad_inventory_matches_todays_inline_query() -> None:
    assert load_gaql_template("ad_inventory") == _build_select(_AD_FIELDS, "ad_group_ad")


def test_campaign_metrics_matches_todays_inline_query() -> None:
    expected = f"SELECT {', '.join(_METRIC_FIELDS)} FROM campaign"  # noqa: S608
    assert load_gaql_template("campaign_metrics") == expected


def test_campaign_budget_lookup_matches_todays_inline_query() -> None:
    expected = "SELECT campaign_budget.resource_name FROM campaign"
    assert load_gaql_template("campaign_budget_lookup") == expected


def test_gaql_templates_are_package_data_resolvable_without_the_checkout() -> None:
    # T214: la imagen importa el paquete INSTALADO; las plantillas viajan como
    # package data y se resuelven con importlib.resources, nunca con
    # `Path(__file__).parents[N]` hacia la raiz del checkout.
    folder = importlib.resources.files("safent_ads.broker.platforms").joinpath("gaql")
    shipped = {entry.name for entry in folder.iterdir() if entry.name.endswith(".gaql")}
    assert shipped == {f"{name}.gaql" for name in GAQL_TEMPLATE_NAMES}
    for name in GAQL_TEMPLATE_NAMES:
        assert load_gaql_template(name).upper().startswith("SELECT")
