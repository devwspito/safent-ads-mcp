"""Versioned workspace contract. Planning declarations are not execution evidence."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from safent_ads.opportunities.domain.campaign_draft import DraftBudget
from safent_ads.proposals.domain.ad_child_creation import _url

CONTRACT_VERSION = 1


class WorkspaceResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    kind: Literal["video", "script", "landing", "tracking", "whatsapp", "document"]
    title: str = Field(min_length=1, max_length=160)
    status: Literal["pending", "in_progress", "ready"] = "pending"
    url: str | None = Field(default=None, max_length=2048)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_url(self) -> "WorkspaceResource":
        if self.url is not None:
            _url(self.url)
        return self


class WorkspaceBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, max_length=2000)
    schedule: str | None = Field(default=None, max_length=500)
    total_budget: DraftBudget | None = None
    notes: str | None = Field(default=None, max_length=8000)
    source_slug: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,160}$")
    resources: Annotated[list[WorkspaceResource], Field(max_length=100)] = []

    @model_validator(mode="after")
    def unique_resources(self) -> "WorkspaceBrief":
        if len({item.key for item in self.resources}) != len(self.resources):
            raise ValueError("Resource keys must be unique")
        if self.title is not None and not self.title.strip():
            raise ValueError("Title cannot be blank")
        return self


def campaign_step(
    draft: dict[str, Any], proposal: dict[str, Any] | None, execution: dict[str, Any] | None
) -> dict[str, Any]:
    """Only persisted provider execution is evidence of creation. Never infer delivery."""
    if execution and execution["outcome"] == "SUCCEEDED":
        state, label = "created", "Creación verificada; consultar estado actual en Campañas"
    elif execution and execution["outcome"] not in {"CLAIMED", "RUNNING"}:
        state, label = "attention", "Revisar el resultado de ejecución"
    elif proposal and proposal["state"] in {"approved", "scheduled", "executing"}:
        state, label = "executing", "Creación autorizada; esperando confirmación"
    elif proposal and proposal["state"] in {"pending", "postponed"}:
        state, label = "approval", "Revisar y aprobar creación en pausa"
    elif proposal:
        state, label = "attention", "Revisar propuesta resuelta"
    elif draft["missing_fields"]:
        state, label = "incomplete", "Completar datos de la campaña"
    else:
        state, label = "ready", "Preparar revisión de creación en pausa"
    return {"state": state, "label": label, "authorizes_spend": False}
