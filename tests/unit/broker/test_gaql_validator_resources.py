"""004 tasks-2.md R6: endurece `run_gaql` en vez de registrar
`google_ads_query` (H-3). Recursos de referencia nuevos (R4/historia 18) y
lista negra de campos de facturacion/token que gana siempre, sin importar
el recurso -- el bróker es el ultimo guardian antes del SDK."""

from __future__ import annotations

import pytest

from safent_ads.broker.platforms.errors import GaqlValidationError
from safent_ads.broker.platforms.gaql_validator import validate_gaql


@pytest.mark.parametrize(
    "resource",
    [
        "conversion_action",
        "language_constant",
        "customer_client",
        "ad_group_ad_asset_view",
        "geo_target_constant",
    ],
)
def test_new_reference_resources_are_allowed(resource: str) -> None:
    validate_gaql(f"SELECT {resource}.resource_name FROM {resource}")  # noqa: S608


@pytest.mark.parametrize(
    "query",
    [
        "SELECT customer.pay_per_conversion_eligibility_failure_reasons FROM customer",
        "SELECT customer.id FROM customer WHERE customer.payments_account_id = '123'",
        "SELECT billing_setup.id FROM billing_setup",
        "SELECT customer.id FROM customer WHERE customer.some_secret_token = 'x'",
        "SELECT customer_user_access.user_id FROM customer_user_access",
        "SELECT campaign.id FROM campaign WHERE campaign.billing_reference = '1'",
    ],
)
def test_from_fuera_de_la_lista_blanca_y_campo_de_facturacion_se_rechazan_en_el_broker(
    query: str,
) -> None:
    with pytest.raises(GaqlValidationError):
        validate_gaql(query)


def test_customer_test_account_is_not_blacklisted() -> None:
    validate_gaql("SELECT customer.test_account FROM customer")
