"""004 tasks-2.md R7: `get_competitor_links` es un constructor **puro** de
dos URL canonicas (Biblioteca de anuncios de Meta y Centro de Transparencia
de Google), sin ninguna llamada de red -- "coste cero y superficie cero"
(regla de no interferencia, punto 2). Entra porque evita que el arnes
invente el formato del enlace.

Valida el dominio (IDNA, minusculas, sin literal IP, sin credenciales,
comparacion con separador de punto) antes de codificarlo en la URL: un
dominio que no respeta la forma de un nombre de host nunca llega a la
cadena final."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import quote

from safent_ads.shared.errors import DomainError

__all__ = [
    "CompetitorLinks",
    "InvalidCountryCodeError",
    "InvalidDomainError",
    "MissingSearchTermError",
    "build_competitor_links",
]

_COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
_LABEL_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_DOMAIN_LENGTH = 253
_MIN_DOMAIN_LABELS = 2

_META_AD_LIBRARY_BASE = "https://www.facebook.com/ads/library/"
_GOOGLE_TRANSPARENCY_BASE = "https://adstransparency.google.com/"


class InvalidCountryCodeError(DomainError):
    """`country` no es un codigo ISO-3166-1 alfa-2 en mayusculas."""


class InvalidDomainError(DomainError):
    """`domain` no tiene forma de nombre de host valido, es una IP literal,
    o lleva credenciales/esquema/ruta incrustados."""


class MissingSearchTermError(DomainError):
    """Ni `domain` ni `name` llegaron: no hay nada que buscar."""


@dataclass(frozen=True, slots=True)
class CompetitorLinks:
    meta_ad_library_url: str
    google_transparency_center_url: str


def build_competitor_links(
    *, domain: str | None, name: str | None, country: str
) -> CompetitorLinks:
    """Puro: ninguna llamada de red. `domain` gana si los dos llegan (mismo
    termino en ambos enlaces, nunca una mezcla de los dos criterios)."""
    _validate_country(country)
    term = _normalize_domain(domain) if domain else _validate_search_name(name)
    encoded = quote(term, safe="")
    return CompetitorLinks(
        meta_ad_library_url=(
            f"{_META_AD_LIBRARY_BASE}?active_status=all&ad_type=all"
            f"&country={country}&q={encoded}"
        ),
        google_transparency_center_url=(
            f"{_GOOGLE_TRANSPARENCY_BASE}?region={country}&domain={encoded}"
            if domain
            else f"{_GOOGLE_TRANSPARENCY_BASE}?region={country}&query={encoded}"
        ),
    )


def _validate_country(country: str) -> None:
    if not _COUNTRY_PATTERN.match(country):
        raise InvalidCountryCodeError(f"country debe ser ISO-3166-1 alfa-2: {country!r}")


def _validate_search_name(name: str | None) -> str:
    if not name or not name.strip():
        raise MissingSearchTermError("se necesita `domain` o `name`")
    return name.strip()


def _normalize_domain(domain: str) -> str:
    candidate = domain.strip().lower()
    if not candidate or len(candidate) > _MAX_DOMAIN_LENGTH:
        raise InvalidDomainError(f"dominio vacio o demasiado largo: {domain!r}")
    if "@" in candidate or "://" in candidate or "/" in candidate:
        raise InvalidDomainError(f"dominio con credenciales, esquema o ruta: {domain!r}")
    if _is_literal_ip(candidate):
        raise InvalidDomainError(f"no se admite una IP literal como dominio: {domain!r}")
    ascii_domain = _to_idna(candidate)
    labels = ascii_domain.split(".")
    if len(labels) < _MIN_DOMAIN_LABELS or any(not _LABEL_PATTERN.match(label) for label in labels):
        raise InvalidDomainError(f"dominio con forma invalida: {domain!r}")
    return ascii_domain


def _is_literal_ip(candidate: str) -> bool:
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _to_idna(candidate: str) -> str:
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomainError(f"dominio no codificable en IDNA: {candidate!r}") from exc
