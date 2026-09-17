"""004 tasks-2.md R4 (historia 18): tres lecturas de referencia de Google.
`list_google_conversion_actions`/`search_google_constants` construyen su
GAQL **en el servidor** a partir de argumentos tipados (nunca GAQL del
llamante: para eso esta `run_gaql`, R6) y se ejecutan con el mismo camino
que ya existe (`AdsPlatformPort.run_gaql` -> `validate_gaql`, R6 ya admite
`conversion_action`/`geo_target_constant`/`language_constant`).

`get_google_keyword_ideas` no es GAQL: `KeywordPlanIdeaService` es un
servicio de generacion, no de consulta -- necesita su propio `Protocol`
estrecho, que el adaptador real implementa (I1)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from safent_ads.mcp.application.reference_data_port import GoogleConstantKind
from safent_ads.shared.errors import DomainError

__all__ = [
    "GoogleKeywordIdeaClient",
    "GoogleReferenceQueryError",
    "build_conversion_actions_query",
    "build_google_constants_query",
    "fetch_keyword_ideas",
]

_MAX_KEYWORD_IDEAS: Final = 200
_MAX_LITERAL_LENGTH: Final = 80
# Texto de busqueda seguro para interpolar en un literal GAQL (GAQL no
# tiene parametros vinculados, threat-model.md T-1): letras, digitos,
# espacio y puntuacion basica; ninguna comilla ni barra invertida.
_SAFE_LITERAL_PATTERN: Final = re.compile(r"^[\w .,'-]{1,80}$")
_COUNTRY_PATTERN: Final = re.compile(r"^[A-Z]{2}$")

_CONVERSION_ACTION_QUERY: Final = (
    "SELECT conversion_action.resource_name, conversion_action.name, "
    "conversion_action.category, conversion_action.status "
    "FROM conversion_action"
)


class GoogleReferenceQueryError(DomainError):
    """`query`/`country` no respetan la forma segura para interpolarse en
    un literal GAQL."""


def build_conversion_actions_query() -> str:
    return _CONVERSION_ACTION_QUERY


def build_google_constants_query(
    *, kind: GoogleConstantKind, query: str, country: str | None
) -> str:
    term = _safe_literal(query)
    if kind is GoogleConstantKind.LANGUAGE:
        # nosec B608 / noqa S608: `term` ya paso por `_safe_literal` (charset
        # cerrado, sin comillas ni barra invertida) -- GAQL no tiene
        # parametros vinculados (threat-model.md T-1).
        # `term` ya paso por `_safe_literal`: seguro para interpolar (T-1).
        select = "SELECT language_constant.resource_name, language_constant.name, "
        select += "language_constant.code FROM language_constant "
        select += f"WHERE language_constant.name LIKE '%{term}%'"  # noqa: S608
        return select
    where = f"geo_target_constant.name LIKE '%{term}%'"
    if country is not None:
        where += f" AND geo_target_constant.country_code = '{_safe_country(country)}'"
    select = "SELECT geo_target_constant.resource_name, geo_target_constant.name, "
    select += "geo_target_constant.country_code FROM geo_target_constant "
    select += f"WHERE {where}"  # noqa: S608 - clausula con literales ya saneados
    return select


def _safe_literal(value: str) -> str:
    if len(value) > _MAX_LITERAL_LENGTH or not _SAFE_LITERAL_PATTERN.match(value):
        raise GoogleReferenceQueryError(f"texto de busqueda no permitido: {value!r}")
    return value


def _safe_country(country: str) -> str:
    if not _COUNTRY_PATTERN.match(country):
        raise GoogleReferenceQueryError(f"country debe ser ISO-3166-1 alfa-2: {country!r}")
    return country


class GoogleKeywordIdeaClient(Protocol):
    """Subconjunto de `KeywordPlanIdeaService` que el bróker necesita. En
    produccion, un wrapper fino sobre el SDK; en tests, un doble sencillo."""

    def generate_keyword_ideas(
        self,
        customer_id: str,
        *,
        seed_keywords: Sequence[str],
        geo_target_constant: str,
        language_constant: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...


async def fetch_keyword_ideas(
    client: GoogleKeywordIdeaClient,
    customer_id: str,
    *,
    seed_keywords: Sequence[str],
    geo_target: str,
    language: str,
) -> list[Mapping[str, Any]]:
    rows = await asyncio.to_thread(
        client.generate_keyword_ideas,
        customer_id,
        seed_keywords=seed_keywords,
        geo_target_constant=geo_target,
        language_constant=language,
        limit=_MAX_KEYWORD_IDEAS,
    )
    return list(rows[:_MAX_KEYWORD_IDEAS])
