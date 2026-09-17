"""`validate_gaql`: solo lectura, una sentencia, recurso en lista blanca
(contracts/platform-port.md, threat-model.md T-1)."""

from __future__ import annotations

import pytest

from safent_ads.broker.platforms.errors import GaqlValidationError
from safent_ads.broker.platforms.gaql_validator import _ALLOWED_RESOURCES, validate_gaql


def test_accepts_simple_select() -> None:
    validate_gaql(
        "SELECT campaign.id, campaign.name FROM campaign WHERE campaign.status = 'ENABLED'"
    )


def test_accepts_lowercase_select() -> None:
    validate_gaql("select campaign.id from campaign")


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE campaign SET status = 'PAUSED'",
        "SELECT campaign.id FROM campaign; DROP TABLE campaign",
        "mutate campaign set status = REMOVED",
        "SELECT campaign.id FROM campaign_budget WHERE 1=1; DELETE FROM campaign",
        "INSERT INTO campaign (id) VALUES (1)",
        "SELECT campaign.id FROM campaign; REMOVE campaign",
    ],
)
def test_gaql_rejects_mutate(query: str) -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql(query)


def test_rejects_non_select_prefix() -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql("EXPLAIN SELECT campaign.id FROM campaign")


def test_rejects_multiple_statements_even_if_both_are_select() -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql("SELECT campaign.id FROM campaign; SELECT ad_group.id FROM ad_group")


def test_rejects_resource_outside_allow_list() -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql("SELECT billing_setup.id FROM billing_setup")


def test_rejects_query_without_from_clause() -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql("SELECT 1")


def test_rejects_oversized_query() -> None:
    huge_where = " OR ".join(["campaign.id = 1"] * 1000)
    oversized_query = f"SELECT campaign.id FROM campaign WHERE {huge_where}"  # noqa: S608
    with pytest.raises(GaqlValidationError):
        validate_gaql(oversized_query)


@pytest.mark.parametrize(
    "resource",
    [
        "asset_group",
        "asset_group_asset",
        "campaign_asset",
        "ad_group_asset",
        "campaign_conversion_goal",
    ],
)
def test_los_cinco_recursos_nuevos_pasan(resource: str) -> None:
    validate_gaql(f"SELECT {resource}.resource_name FROM {resource}")  # noqa: S608


def test_la_lista_blanca_es_exactamente_esta() -> None:
    assert _ALLOWED_RESOURCES == {
        "customer",
        "customer_client",
        "campaign",
        "campaign_budget",
        "campaign_criterion",
        "geo_target_constant",
        "language_constant",
        "conversion_action",
        "ad_group",
        "ad_group_ad",
        "ad_group_ad_asset_view",
        "ad_group_criterion",
        "keyword_view",
        "search_term_view",
        "asset",
        "asset_group",
        "asset_group_asset",
        "campaign_asset",
        "ad_group_asset",
        "campaign_conversion_goal",
    }


def test_audience_y_user_list_nunca_se_leen() -> None:
    assert "audience" not in _ALLOWED_RESOURCES
    assert "user_list" not in _ALLOWED_RESOURCES
    assert "customer_negative_criterion" not in _ALLOWED_RESOURCES
    with pytest.raises(GaqlValidationError):
        validate_gaql("SELECT audience.id FROM audience")
    with pytest.raises(GaqlValidationError):
        validate_gaql("SELECT user_list.id FROM user_list")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT campaign.id, user_list_criterion.user_list FROM campaign",
        "SELECT campaign.id, campaign_audience_view.resource_name FROM campaign",
        "SELECT customer_client.descriptive_name FROM customer_client",
    ],
)
def test_campos_de_publico_y_listas_de_clientes_rechazados(query: str) -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql(query)
