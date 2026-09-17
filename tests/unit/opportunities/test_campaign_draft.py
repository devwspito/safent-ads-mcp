import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.campaign_draft_tools import DraftSaveArgs
from safent_ads.opportunities.domain.campaign_draft import (
    DraftError,
    DraftFields,
    completed_brief,
    default_creation_plan,
)

_GOOGLE_BASE_FIELDS = {
    "title": "Reserva de citas",
    "platform": "google",
    "daily_budget": {"amount": "20.00", "currency": "EUR"},
}


def test_missing_budget_and_destination_are_never_invented():
    fields = DraftFields(title="Owner plan")
    assert fields.daily_budget is None and fields.landing_url is None
    with pytest.raises(DraftError, match="INCOMPLETE") as error:
        completed_brief(fields)
    assert {"daily_budget", "landing_url"} <= set(error.value.missing)


def test_multiline_owner_notes_are_stored_as_data_not_executable_content():
    assert DraftFields(notes="First line\nSecond line").notes == "First line\nSecond line"
    with pytest.raises(ValidationError):
        DraftFields(notes="Invalid\x00note")


@pytest.mark.parametrize(
    "change",
    [
        {"landing_url": "http://example.com"},
        {"image_url": "https://127.0.0.1/x"},
        {"daily_budget": {"amount": "20"}},
        {"daily_budget": {"amount": 20, "currency": "EUR"}},
        {"approved": True},
        {"creation_plan": {}},
        {"title": " "},
        {"meta_page_id": "app-secret"},
    ],
)
def test_strict_draft_fields_reject_unsafe_or_inferred_shapes(change):
    with pytest.raises(ValidationError):
        DraftFields.model_validate(change)


def test_mcp_allows_only_validated_planning_urls_and_strict_revision():
    data = {
        "business_id": "11111111-1111-1111-1111-111111111111",
        "draft_key": "idea",
        "changes": {"landing_url": "https://example.com/reserve"},
    }
    assert DraftSaveArgs.model_validate(data).changes.landing_url == "https://example.com/reserve"
    with pytest.raises(ValidationError):
        DraftSaveArgs.model_validate(data | {"expected_revision": True})


def test_sin_canal_el_borrador_sigue_siendo_search():
    fields = DraftFields.model_validate(_GOOGLE_BASE_FIELDS)
    assert fields.google_channel_type is None

    plan = default_creation_plan(fields)

    assert plan["native"] == {
        "advertising_channel_type": "SEARCH",
        "bidding_strategy": "MANUAL_CPC",
        "network_settings": {
            "target_google_search": True,
            "target_search_network": True,
            "target_content_network": False,
            "target_partner_search_network": False,
        },
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }


@pytest.mark.parametrize("channel", ["DEMAND_GEN", "PERFORMANCE_MAX"])
def test_pmax_sin_metas_es_draft_incomplete(channel: str):
    fields = DraftFields.model_validate({**_GOOGLE_BASE_FIELDS, "google_channel_type": channel})

    with pytest.raises(DraftError, match="INCOMPLETE") as error:
        default_creation_plan(fields)

    assert error.value.missing == ("conversion_goals",)


def test_performance_max_con_metas_lleva_los_literales_forzados():
    fields = DraftFields.model_validate(
        {
            **_GOOGLE_BASE_FIELDS,
            "google_channel_type": "PERFORMANCE_MAX",
            "conversion_goals": ["customers/1234567890/conversionActions/1"],
        }
    )

    plan = default_creation_plan(fields)

    assert plan["native"]["advertising_channel_type"] == "PERFORMANCE_MAX"
    assert plan["native"]["bidding_strategy"] == {"kind": "MAXIMIZE_CONVERSIONS"}
    assert plan["native"]["url_expansion_opt_out"] is True
    assert plan["native"]["text_asset_automation_enabled"] is False
    assert "network_settings" not in plan["native"]


def test_display_no_lleva_redes_y_usa_puja_etiquetada():
    fields = DraftFields.model_validate({**_GOOGLE_BASE_FIELDS, "google_channel_type": "DISPLAY"})

    plan = default_creation_plan(fields)

    assert plan["native"]["bidding_strategy"] == {"kind": "MANUAL_CPC"}
    assert "network_settings" not in plan["native"]
    assert "url_expansion_opt_out" not in plan["native"]
