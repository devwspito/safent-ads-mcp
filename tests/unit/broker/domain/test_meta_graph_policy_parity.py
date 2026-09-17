"""B-3 (revision de seguridad de las herramientas sensibles del MCP): el
bróker (`broker/domain/meta_graph_policy.py`) y ads-api
(`mcp/domain/meta_graph_path.py`) mantienen, a proposito, DOS copias
independientes de la misma politica de `get_meta_graph` (bounded contexts
distintos, procesos distintos -- ver docstring de cada modulo). Esta
prueba es el unico lugar que impide que diverjan sin que salte en CI: si
alguien anade una arista, un campo o una clave de params a un lado y se
olvida del otro, esta prueba falla."""

from __future__ import annotations

from safent_ads.broker.domain import meta_graph_policy as broker_policy
from safent_ads.mcp.domain import meta_graph_path as mcp_policy


def test_las_tablas_de_aristas_y_campos_son_identicas_en_broker_y_mcp() -> None:
    assert broker_policy._ALLOWED_EDGES == mcp_policy._ALLOWED_EDGES
    assert broker_policy._FIELDS_BY_EDGE == mcp_policy._FIELDS_BY_EDGE


def test_las_listas_negras_de_campos_son_identicas_en_broker_y_mcp() -> None:
    assert broker_policy._DENY_EXACT_FIELDS == mcp_policy._DENY_EXACT_FIELDS
    assert broker_policy._DENY_FIELD_PREFIXES == mcp_policy._DENY_FIELD_PREFIXES
    assert broker_policy._DENY_FIELD_SUFFIXES == mcp_policy._DENY_FIELD_SUFFIXES


def test_la_lista_negra_de_claves_de_params_es_identica_en_broker_y_mcp() -> None:
    assert broker_policy._DENY_PARAM_KEYS == mcp_policy._DENY_PARAM_KEYS


def test_los_topes_numericos_son_identicos_en_broker_y_mcp() -> None:
    assert broker_policy._MAX_ROWS == mcp_policy._MAX_ROWS
    assert broker_policy._MAX_RESPONSE_BYTES == mcp_policy._MAX_RESPONSE_BYTES
    assert broker_policy._MAX_PARAM_KEYS == mcp_policy._MAX_PARAM_KEYS
    assert broker_policy._MAX_PARAM_VALUE_LENGTH == mcp_policy._MAX_PARAM_VALUE_LENGTH
    assert broker_policy._MAX_SINCE_UNTIL_DAYS == mcp_policy._MAX_SINCE_UNTIL_DAYS


def test_los_patrones_de_node_y_clave_de_param_son_identicos_en_broker_y_mcp() -> None:
    assert broker_policy._NODE_PATTERN.pattern == mcp_policy._NODE_PATTERN.pattern
    assert broker_policy._PARAM_KEY_PATTERN.pattern == mcp_policy._PARAM_KEY_PATTERN.pattern
