"""Search targeting fields, visible in the approved MCP plan."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class GoogleKeywordArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    text: Annotated[str, Field(min_length=1, max_length=80)]
    match_type: Literal["EXACT", "PHRASE", "BROAD"]


class GoogleGeographicTargetingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    geo_target_constants: Annotated[
        list[Annotated[str, Field(pattern=r"^geoTargetConstants/[1-9][0-9]{0,19}$")]],
        Field(min_length=1, max_length=25),
    ]
    positive_geo_target_type: Literal["PRESENCE"]
