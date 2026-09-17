"""`AuthorizedEmailList` (002b tasks.md T019/T021, FR-105): conjunto cerrado
de direcciones COMPLETAS. Vacia = nadie (nunca "vacia = todos"); ni dominios
sueltos ni comodines autorizan; una entrada mal escrita se descarta en vez de
impedir el arranque (edge case "lista vacia o mal escrita")."""

from __future__ import annotations

from safent_ads.iam.application.ports import AuthorizedEmailList
from safent_ads.iam.domain.email import Email

_OWNER = Email("duenyo@example.com")


def test_an_empty_list_authorizes_nobody() -> None:
    allow_list = AuthorizedEmailList.from_raw(())

    assert allow_list.is_empty is True
    assert allow_list.authorizes(_OWNER) is False


def test_a_listed_address_is_authorized() -> None:
    allow_list = AuthorizedEmailList.from_raw(("duenyo@example.com",))

    assert allow_list.authorizes(_OWNER) is True


def test_the_comparison_ignores_case_and_surrounding_blanks() -> None:
    allow_list = AuthorizedEmailList.from_raw(("  DuenYo@Example.Com  ",))

    assert allow_list.authorizes(Email("DUENYO@EXAMPLE.COM")) is True


def test_an_unlisted_address_is_not_authorized() -> None:
    allow_list = AuthorizedEmailList.from_raw(("duenyo@example.com",))

    assert allow_list.authorizes(Email("otra@example.com")) is False


def test_a_bare_domain_authorizes_nobody() -> None:
    allow_list = AuthorizedEmailList.from_raw(("example.com",))

    assert allow_list.is_empty is True
    assert allow_list.authorizes(_OWNER) is False


def test_an_at_prefixed_domain_authorizes_nobody() -> None:
    allow_list = AuthorizedEmailList.from_raw(("@example.com",))

    assert allow_list.is_empty is True
    assert allow_list.authorizes(_OWNER) is False


def test_a_wildcard_authorizes_nobody_in_its_domain() -> None:
    allow_list = AuthorizedEmailList.from_raw(("*@example.com",))

    assert allow_list.authorizes(_OWNER) is False


def test_malformed_entries_are_discarded_without_closing_the_valid_ones() -> None:
    allow_list = AuthorizedEmailList.from_raw(("example.com", "", "   ", "duenyo@example.com"))

    assert allow_list.authorizes(_OWNER) is True
    assert allow_list.is_empty is False
