"""004 tasks-2.md R3/R4 (historia 18): datos de referencia de Meta y Google,
cada uno sobre una arista/consulta concreta ya conocida -- nunca un paso a
traves libre (eso es R5). Modulo autonomo: sus propios DTOs, sin tocar
`mcp/application/ports.py`/`dto.py` compartidos.

Puro: sin I/O, sin framework."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "GoogleConstant",
    "GoogleConstantKind",
    "GoogleConversionAction",
    "GoogleKeywordIdea",
    "GoogleReferenceDataPort",
    "MetaAudience",
    "MetaCatalog",
    "MetaPage",
    "MetaPixel",
    "MetaReachEstimate",
    "MetaReferenceDataPort",
    "MetaTargetingKind",
    "MetaTargetingSuggestion",
]


# --- Meta (R3) ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetaPage:
    page_id: str
    name: str
    instagram_business_account_id: str | None


@dataclass(frozen=True, slots=True)
class MetaPixel:
    pixel_id: str
    name: str
    last_fired_event_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetaAudience:
    audience_id: str
    name: str
    kind: str
    approximate_count_upper_bound: int | None


@dataclass(frozen=True, slots=True)
class MetaCatalog:
    catalog_id: str
    name: str
    product_count: int | None


class MetaTargetingKind(StrEnum):
    INTEREST = "adinterest"
    DEMOGRAPHIC = "adTargetingCategory"


@dataclass(frozen=True, slots=True)
class MetaTargetingSuggestion:
    targeting_id: str
    name: str
    audience_size_lower_bound: int | None
    audience_size_upper_bound: int | None


@dataclass(frozen=True, slots=True)
class MetaReachEstimate:
    users_lower_bound: int | None
    users_upper_bound: int | None
    estimate_ready: bool


class MetaReferenceDataPort(Protocol):
    async def list_meta_pages(
        self, business_id: str, account_ref: str
    ) -> tuple[MetaPage, ...]: ...

    async def list_meta_pixels(
        self, business_id: str, account_ref: str
    ) -> tuple[MetaPixel, ...]: ...

    async def list_meta_audiences(
        self, business_id: str, account_ref: str
    ) -> tuple[MetaAudience, ...]: ...

    async def list_meta_catalogs(
        self, business_id: str, account_ref: str
    ) -> tuple[MetaCatalog, ...]: ...

    async def search_meta_targeting(
        self, business_id: str, account_ref: str, *, kind: MetaTargetingKind, query: str
    ) -> tuple[MetaTargetingSuggestion, ...]: ...

    async def get_meta_reach_estimate(
        self,
        business_id: str,
        account_ref: str,
        *,
        optimization_goal: str,
        countries: tuple[str, ...],
    ) -> MetaReachEstimate: ...


# --- Google (R4) ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoogleConversionAction:
    resource_name: str
    name: str
    category: str
    status: str


class GoogleConstantKind(StrEnum):
    GEO_TARGET = "geo_target"
    LANGUAGE = "language"


@dataclass(frozen=True, slots=True)
class GoogleConstant:
    resource_name: str
    name: str
    code: str


@dataclass(frozen=True, slots=True)
class GoogleKeywordIdea:
    text: str
    avg_monthly_searches: int | None
    competition: str | None


class GoogleReferenceDataPort(Protocol):
    async def list_google_conversion_actions(
        self, business_id: str, account_ref: str
    ) -> tuple[GoogleConversionAction, ...]: ...

    async def search_google_constants(
        self,
        business_id: str,
        account_ref: str,
        *,
        kind: GoogleConstantKind,
        query: str,
        country: str | None,
    ) -> tuple[GoogleConstant, ...]: ...

    async def get_google_keyword_ideas(
        self,
        business_id: str,
        account_ref: str,
        *,
        seed_keywords: tuple[str, ...],
        geo_target: str,
        language: str,
    ) -> tuple[GoogleKeywordIdea, ...]: ...
