"""Closed result contract shared by MCP, the panel and local runtime adapters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from safent_ads.opportunities.domain.campaign_draft import DraftFields

MAX_BLOCKER_LENGTH = 500


class RuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["prepared", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=2000)
    blockers: list[str] = Field(max_length=30)
    campaign: DraftFields | None
    expected_draft_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def evidence_required(self) -> "RuntimeResult":
        if any(not item.strip() or len(item) > MAX_BLOCKER_LENGTH for item in self.blockers):
            raise ValueError("Each blocker must contain 1-500 characters")
        if self.outcome == "prepared" and (self.campaign is None or self.blockers):
            raise ValueError("Prepared requires a persisted campaign and no preparation blockers")
        if self.campaign is not None and not self.campaign.title:
            raise ValueError("A campaign draft requires a title")
        if self.outcome != "prepared" and not self.blockers:
            raise ValueError("Explain the blocking condition")
        # Runtime preparation does not accept executable native plans. Those
        # belong to the existing human-reviewed proposal/execution pipeline.
        if self.campaign is not None and self.campaign.creation_plan is not None:
            raise ValueError("Preparation accepts draft fields, not native execution plans")
        return self
