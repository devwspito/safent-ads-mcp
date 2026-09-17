"""Modelos pydantic estrictos (T045, threat-model.md C-11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.args import (
    GetCampaignArgs,
    GetPortfolioOverviewArgs,
    ListCampaignsArgs,
    ListSignalsArgs,
    ProposeCreativePublicationArgs,
    ProposeTargetingChangeArgs,
    RunGaqlArgs,
    SearchDecisionLogArgs,
)

_BIZ = "11111111-1111-1111-1111-111111111111"


def test_tool_args_reject_free_url() -> None:
    """C-11: "sin URLs libres". Un `entity_ref` con un esquema de red
    embebido en el `external_id` no debe validar, aunque el patron de
    `EntityRef` por si solo lo permitiria (`external_id` acepta `.+`)."""
    with pytest.raises(ValidationError):
        GetCampaignArgs(business_id=_BIZ, entity_ref="google:campaign:http://evil.example/x")


def test_tool_args_reject_free_url_in_gaql_query_field() -> None:
    with pytest.raises(ValidationError):
        RunGaqlArgs(
            business_id=_BIZ,
            account_ref="123-456",
            query="SELECT campaign.id FROM campaign WHERE x = 'https://evil.example'",
        )


def _nest_url(depth: int, url: str = "https://evil.tld") -> dict:
    value: object = url
    for _ in range(depth):
        value = {"k": value}
    return value  # type: ignore[return-value]


@pytest.mark.parametrize("depth", [9, 12])
def test_r2_targeting_diff_con_url_anidada_mas_alla_del_tope_falla_cerrado(depth: int) -> None:
    """R-2: `_reject_free_urls` recortaba en `return` al superar la
    profundidad maxima en vez de denegar -- una URL a profundidad 9 o 12
    dentro de `targeting_diff` (`dict[str, Any]` sin tope propio) pasaba de
    largo. Debe fallar cerrado (`raise`), no abierto (`return`)."""
    with pytest.raises(ValidationError):
        ProposeTargetingChangeArgs(
            business_id=_BIZ,
            entity_ref="meta:campaign:1",
            targeting_diff=_nest_url(depth),
            cause={"text": "motivo de prueba"},
        )


@pytest.mark.parametrize("depth", [9, 12])
def test_r2_ad_copy_con_url_anidada_mas_alla_del_tope_falla_cerrado(depth: int) -> None:
    with pytest.raises(ValidationError):
        ProposeCreativePublicationArgs(
            business_id=_BIZ,
            ad_set_ref="meta:ad_set:1",
            creative_asset_ids=["abc"],
            ad_copy=_nest_url(depth),
            cause={"text": "motivo de prueba"},
        )


def test_enum_strict() -> None:
    """`WindowPreset` es un enum cerrado: un valor fuera del catalogo
    (`13D`) debe fallar, no aceptarse silenciosamente como texto libre."""
    with pytest.raises(ValidationError):
        GetPortfolioOverviewArgs(business_id=_BIZ, window={"preset": "13D"})


def test_enum_strict_accepts_documented_values() -> None:
    args = GetPortfolioOverviewArgs(business_id=_BIZ, window={"preset": "7D"})
    assert args.window.preset.value == "7D"


def test_enum_strict_rejects_unknown_campaign_status() -> None:
    with pytest.raises(ValidationError):
        ListCampaignsArgs(business_id=_BIZ, status="zombified")


def test_enum_strict_rejects_unknown_signal_kind() -> None:
    with pytest.raises(ValidationError):
        ListSignalsArgs(business_id=_BIZ, kind="maybe")


def test_business_id_must_be_uuid() -> None:
    with pytest.raises(ValidationError):
        ListCampaignsArgs(business_id="not-a-uuid")


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        ListCampaignsArgs(business_id=_BIZ, unexpected_field="x")


def test_window_rejects_preset_and_range_together() -> None:
    with pytest.raises(ValidationError):
        GetPortfolioOverviewArgs(
            business_id=_BIZ,
            window={"preset": "7D", "from": "2026-01-01", "to": "2026-01-07"},
        )


def test_run_gaql_rejects_non_select() -> None:
    with pytest.raises(ValidationError):
        RunGaqlArgs(business_id=_BIZ, account_ref="123", query="UPDATE campaign SET x = 1")


def test_search_decision_log_rejects_since_after_until() -> None:
    with pytest.raises(ValidationError):
        SearchDecisionLogArgs(
            business_id=_BIZ,
            since="2026-02-01T00:00:00Z",
            until="2026-01-01T00:00:00Z",
        )
