"""Value object `ConversionGoal` (data-model.md §`ConversionGoal`; contracts/
mcp-tools.md §2 `ConversionGoalArgs`). Shape only, no I/O, no clock: account
membership and `ENABLED` status are boundary checks the broker performs by
re-reading GAQL before writing (T032), never here."""

from __future__ import annotations

import re
from dataclasses import dataclass

_RESOURCE_NAME_PATTERN = re.compile(r"^customers/[0-9]{1,20}/conversionActions/[0-9]{1,20}$")


class ConversionGoalError(ValueError):
    """A stable code, never provider input or a credential."""


@dataclass(frozen=True, slots=True)
class ConversionGoal:
    resource_name: str

    def __post_init__(self) -> None:
        if not _RESOURCE_NAME_PATTERN.fullmatch(self.resource_name):
            raise ConversionGoalError("conversion_goal_resource_name_invalid")

    @property
    def customer_id(self) -> str:
        return self.resource_name.split("/")[1]
