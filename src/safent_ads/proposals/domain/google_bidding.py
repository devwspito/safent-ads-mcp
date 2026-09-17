"""Google Ads bidding as tagged value objects (data-model.md §Pujas;
contracts/mcp-tools.md §2 `GoogleBiddingArgs`). `target_roas` is a ratio --
euros of value per euro spent -- and never passes through `Money` (INV-17):
a validator of money never accepts it, and its own validator never accepts
`{amount, currency}` shape."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from safent_ads.proposals.domain.money import Money

_TARGET_ROAS_MIN = Decimal("0.01")
_TARGET_ROAS_MAX = Decimal("100.00")
_TARGET_ROAS_MAX_DECIMAL_PLACES = 4


class GoogleBiddingError(ValueError):
    """A stable code, never provider input or a credential."""


@dataclass(frozen=True, slots=True)
class ManualCpc:
    """No objective; legal only for the channels whose row admits it (SEARCH, DISPLAY)."""

    @property
    def is_conversion_based(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MaximizeClicks:
    cpc_bid_ceiling: Money | None = None

    def __post_init__(self) -> None:
        if self.cpc_bid_ceiling is not None and not self.cpc_bid_ceiling.is_positive():
            raise GoogleBiddingError("google_bidding_cpc_bid_ceiling_not_positive")

    @property
    def is_conversion_based(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MaximizeConversions:
    target_cpa: Money | None = None

    def __post_init__(self) -> None:
        if self.target_cpa is not None and not self.target_cpa.is_positive():
            raise GoogleBiddingError("google_bidding_target_cpa_not_positive")

    @property
    def is_conversion_based(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class MaximizeConversionValue:
    target_roas: Decimal | None = None

    def __post_init__(self) -> None:
        if self.target_roas is not None:
            _require_valid_target_roas(self.target_roas)

    @property
    def is_conversion_based(self) -> bool:
        return True


def _require_valid_target_roas(target_roas: Decimal) -> None:
    # `isinstance`, not a duck-typed check: rejects a Money-shaped mapping or
    # a Money instance outright, which is the point of INV-17.
    if not isinstance(target_roas, Decimal) or not target_roas.is_finite():
        raise GoogleBiddingError("google_bidding_target_roas_invalid")
    if not _TARGET_ROAS_MIN <= target_roas <= _TARGET_ROAS_MAX:
        raise GoogleBiddingError("google_bidding_target_roas_out_of_range")
    exponent = target_roas.as_tuple().exponent
    if not isinstance(exponent, int):
        raise GoogleBiddingError("google_bidding_target_roas_invalid")
    decimal_places = max(0, -exponent)
    if decimal_places > _TARGET_ROAS_MAX_DECIMAL_PLACES:
        raise GoogleBiddingError("google_bidding_target_roas_precision_invalid")


GoogleBidding = ManualCpc | MaximizeClicks | MaximizeConversions | MaximizeConversionValue
