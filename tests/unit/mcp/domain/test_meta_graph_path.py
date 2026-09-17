"""`meta_graph_path.py` (R5, S-1): unica fuente de verdad de la politica de
`get_meta_graph`. Tabla completa de rutas permitidas y denegadas."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from safent_ads.mcp.domain.meta_graph_path import (
    GraphEdgeDeniedError,
    GraphFieldDeniedError,
    GraphNodeFormatError,
    GraphParamsDeniedError,
    node_is_account_itself,
    truncate_response,
    validate_edge,
    validate_fields,
    validate_node_shape,
    validate_params,
)

_TODAY = date(2026, 9, 14)


def test_tabla_de_rutas_permitidas_y_denegadas() -> None:
    # Arista de la lista blanca: permitida.
    validate_edge("campaigns")
    validate_fields("campaigns", ["id", "name", "status"])

    # Arista fuera de la lista blanca => denegada.
    with pytest.raises(GraphEdgeDeniedError):
        validate_edge("adaccounts")

    # `fields=access_token` => denegada aunque la arista lo permita (la
    # lista negra gana siempre; se prueba contra una arista real permitida).
    validate_edge("campaigns")
    with pytest.raises(GraphFieldDeniedError):
        validate_fields("campaigns", ["access_token"])

    # `node` de otra cuenta: comparacion pura (la resolucion real via Graph
    # API vive en infraestructura, I/O) -- act_111 no es act_222.
    assert node_is_account_itself("act_111", "act_222") is False
    assert node_is_account_itself("act_111", "act_111") is True

    # `params` con `access_token` => denegada.
    with pytest.raises(GraphParamsDeniedError):
        validate_params({"access_token": "EAAxxx"}, today=_TODAY)

    # 201 filas => truncado con aviso.
    rows = [{"id": str(i)} for i in range(201)]
    result = truncate_response(rows)
    assert len(result.rows) == 200
    assert result.truncated is True


@pytest.mark.parametrize(
    "edge",
    [
        "promote_pages",
        "instagram_accounts",
        "adspixels",
        "customaudiences",
        "saved_audiences",
        "product_catalogs",
        "campaigns",
        "adsets",
        "ads",
        "adcreatives",
        "adimages",
        "advideos",
        "insights",
        "adrules_library",
        "",
    ],
)
def test_all_fourteen_edges_and_empty_string_are_allowed(edge: str) -> None:
    validate_edge(edge)


@pytest.mark.parametrize(
    "edge", ["adaccounts", "instagram_business_accounts", "leadgen_forms", "campaign_group"]
)
def test_edges_outside_the_allow_list_are_denied(edge: str) -> None:
    with pytest.raises(GraphEdgeDeniedError):
        validate_edge(edge)


def test_bj3_the_denied_edge_error_never_echoes_the_raw_value() -> None:
    """Bj-3: el mensaje de error no debe repetir lo que mando el llamante,
    aunque el borde pydantic ya lo acote a `[a-z_]{0,40}` -- el mensaje de
    dominio tiene que ser seguro de mostrar por si mismo."""
    with pytest.raises(GraphEdgeDeniedError) as excinfo:
        validate_edge("leadgen_forms")

    assert "leadgen_forms" not in str(excinfo.value)


@pytest.mark.parametrize(
    "field",
    [
        "access_token",
        "page_access_token",
        "system_user_token",
        "business",
        "users",
        "assigned_users",
        "agencies",
        "io_number",
        "tos_accepted",
        "owner_id",
        "funding_source_details",
        "credit_card_id",
        "extended_credit_line",
        "billing_event",
        "user_id",
    ],
)
def test_denied_fields_win_even_on_an_edge_that_would_allow_it(field: str) -> None:
    with pytest.raises(GraphFieldDeniedError):
        validate_fields("campaigns", ["id", field])


def test_field_outside_the_edge_allow_list_is_denied() -> None:
    with pytest.raises(GraphFieldDeniedError):
        validate_fields("campaigns", ["not_a_real_field"])


def test_field_from_another_edge_allow_list_is_denied() -> None:
    with pytest.raises(GraphFieldDeniedError):
        validate_fields("campaigns", ["subtype"])  # campo de customaudiences


@pytest.mark.parametrize("node", ["act_123", "123", "act_0"])
def test_valid_node_shapes_are_accepted(node: str) -> None:
    validate_node_shape(node)


@pytest.mark.parametrize(
    "node", ["act_", "abc", "123abc", "act_123/insights", "http://evil", ""]
)
def test_invalid_node_shapes_are_rejected(node: str) -> None:
    with pytest.raises(GraphNodeFormatError):
        validate_node_shape(node)


def test_too_many_param_keys_are_rejected() -> None:
    params = {f"k{i}": "v" for i in range(11)}
    with pytest.raises(GraphParamsDeniedError):
        validate_params(params, today=_TODAY)


def test_param_key_with_uppercase_or_symbols_is_rejected() -> None:
    with pytest.raises(GraphParamsDeniedError):
        validate_params({"Bad-Key": "v"}, today=_TODAY)


@pytest.mark.parametrize(
    "key",
    [
        "fields",
        "ids",
        "method",
        "access_token",
        "after",
        "before",
        "limit",
        "format",
        "callback",
        "redirect",
        "appsecret_proof",
    ],
)
def test_params_con_fields_ids_method_o_limit_se_deniegan(key: str) -> None:
    """B-1: `params={"fields": "id,access_token"}` no debe poder pisar la
    lista blanca de campos de la arista ni sacar tokens de pagina."""
    with pytest.raises(GraphParamsDeniedError):
        validate_params({key: "cualquier-valor"}, today=_TODAY)


def test_param_value_over_256_chars_is_rejected() -> None:
    with pytest.raises(GraphParamsDeniedError):
        validate_params({"q": "x" * 257}, today=_TODAY)


@pytest.mark.parametrize("value", [1, 1.5, True])
def test_scalar_non_string_param_values_are_accepted(value: object) -> None:
    validate_params({"q": value}, today=_TODAY)


@pytest.mark.parametrize("value", [{"nested": "dict"}, ["a", "list"], None])
def test_non_scalar_or_none_param_values_are_rejected(value: object) -> None:
    """M-2: `params` solo admite escalares -- un dict o una lista sin techo
    es una via abierta para expansion anidada o payloads sin cota."""
    with pytest.raises(GraphParamsDeniedError):
        validate_params({"q": value}, today=_TODAY)


def test_since_until_within_400_days_is_accepted() -> None:
    since = (_TODAY - timedelta(days=399)).isoformat()
    validate_params({"since": since}, today=_TODAY)


def test_since_until_outside_400_days_is_rejected() -> None:
    since = (_TODAY - timedelta(days=401)).isoformat()
    with pytest.raises(GraphParamsDeniedError):
        validate_params({"since": since}, today=_TODAY)


def test_since_with_non_date_value_is_rejected() -> None:
    with pytest.raises(GraphParamsDeniedError):
        validate_params({"since": "not-a-date"}, today=_TODAY)


def test_truncate_response_caps_by_byte_size_even_under_200_rows() -> None:
    huge_rows = [{"id": str(i), "blob": "x" * 2000} for i in range(100)]
    result = truncate_response(huge_rows)
    assert len(result.rows) < 100
    assert result.truncated is True


def test_truncate_response_does_not_truncate_a_small_response() -> None:
    rows = [{"id": "1"}, {"id": "2"}]
    result = truncate_response(rows)
    assert result.rows == ({"id": "1"}, {"id": "2"})
    assert result.truncated is False
