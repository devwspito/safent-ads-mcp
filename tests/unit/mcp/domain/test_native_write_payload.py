"""`validate_native_write_payload` (004 tasks-2.md W3): tabla completa de
payloads aceptados y rechazados. Puro, sin dobles ni I/O."""

from __future__ import annotations

import pytest

from safent_ads.mcp.domain.native_write_payload import (
    NativeWritePayloadError,
    validate_native_write_payload,
)


def test_un_payload_llano_de_escalares_y_listas_pasa() -> None:
    validate_native_write_payload({"headline": "Nuevo anuncio", "tags": ["a", "b"], "active": True})


def test_un_payload_anidado_hasta_profundidad_cuatro_pasa() -> None:
    validate_native_write_payload({"a": {"b": {"c": {"d": "x"}}}})


class TestTablaDePayloadsAceptadosYRechazados:
    def test_daily_budget_se_rechaza_con_el_nombre_de_la_herramienta_correcta(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"daily_budget": {"amount": "10.00"}})

        assert excinfo.value.code == "USE_BUDGET_TOOL"
        assert "propose_budget_change" in str(excinfo.value)

    def test_bid_amount_se_rechaza_con_el_nombre_de_la_herramienta_correcta(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"bid_amount": "1.50"})

        assert excinfo.value.code == "USE_BID_TOOL"
        assert "propose_bid_target" in str(excinfo.value)

    def test_bid_prefijado_tambien_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"bid_strategy": "manual"})

        assert excinfo.value.code == "USE_BID_TOOL"

    def test_access_token_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"access_token": "abc123"})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_una_credencial_anidada_tambien_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"config": {"page_credential": "abc"}})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_status_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"status": "PAUSED"})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_special_ad_categories_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError):
            validate_native_write_payload({"special_ad_categories": ["HOUSING"]})

    def test_owner_prefijado_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError):
            validate_native_write_payload({"owner_id": "123"})

    def test_funding_source_prefijado_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError):
            validate_native_write_payload({"funding_source_id": "123"})

    def test_billing_prefijado_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError):
            validate_native_write_payload({"billing_event": "IMPRESSIONS"})

    def test_configured_status_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"configured_status": "PAUSED"})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_effective_status_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"effective_status": "PAUSED"})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_spend_cap_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError):
            validate_native_write_payload({"spend_cap": "500"})

    @pytest.mark.parametrize(
        "key", ["daily_spend_cap", "lifetime_spend_cap", "daily_min_spend_target"]
    )
    def test_sufijos_de_gasto_se_rechazan(self, key: str) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({key: "500"})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_pacing_prefijado_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError):
            validate_native_write_payload({"pacing_type": "day_parting"})

    @pytest.mark.parametrize("key", ["budget_rebalance_flag", "start_time", "end_time"])
    def test_r1_claves_de_ritmo_y_ventana_de_campana_se_rechazan(self, key: str) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({key: "2026-01-01T00:00:00+0000"})

        assert excinfo.value.code == "FORBIDDEN_FIELD"

    def test_nueve_kib_se_rechaza(self) -> None:
        payload = {"note": "x" * (9 * 1024)}

        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload(payload)

        assert excinfo.value.code == "PAYLOAD_TOO_LARGE"

    def test_profundidad_cinco_se_rechaza(self) -> None:
        payload = {"a": {"b": {"c": {"d": {"e": "x"}}}}}

        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload(payload)

        assert excinfo.value.code == "PAYLOAD_TOO_DEEP"

    def test_clave_en_mayusculas_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"Headline": "x"})

        assert excinfo.value.code == "INVALID_KEY"

    def test_una_lista_de_objetos_se_rechaza(self) -> None:
        with pytest.raises(NativeWritePayloadError) as excinfo:
            validate_native_write_payload({"items": [{"nested": "no"}]})

        assert excinfo.value.code == "INVALID_VALUE"
