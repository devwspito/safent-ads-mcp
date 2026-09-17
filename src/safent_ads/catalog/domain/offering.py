"""Owner-supplied catalog facts; no inferred price or platform action."""

import re
from dataclasses import dataclass

_TITLE_MAX_LENGTH = 200
_FIRST_PRINTABLE_ASCII = 32
_DELETE_ASCII = 127


@dataclass(frozen=True, slots=True)
class OfferingDetails:
    code: str
    title: str
    price_amount: str | None = None
    price_currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.code
        ):
            raise ValueError("OFFERING_CODE_INVALID")
        if not isinstance(self.title, str) or not 1 <= len(self.title.strip()) <= _TITLE_MAX_LENGTH:
            raise ValueError("OFFERING_TITLE_INVALID")
        if self.title != self.title.strip() or any(
            ord(char) < _FIRST_PRINTABLE_ASCII or ord(char) == _DELETE_ASCII for char in self.title
        ):
            raise ValueError("OFFERING_TITLE_INVALID")
        if (self.price_amount is None) != (self.price_currency is None):
            raise ValueError("OFFERING_PRICE_PAIR_REQUIRED")
        if self.price_amount is not None:
            if not isinstance(self.price_amount, str) or not re.fullmatch(
                r"[0-9]{1,10}(\.[0-9]{1,2})?", self.price_amount
            ):
                raise ValueError("OFFERING_PRICE_INVALID")
            if not isinstance(self.price_currency, str) or not re.fullmatch(
                r"[A-Z]{3}", self.price_currency
            ):
                raise ValueError("OFFERING_CURRENCY_INVALID")
