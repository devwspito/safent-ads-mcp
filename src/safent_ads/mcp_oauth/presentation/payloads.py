"""DTOs de `/api/v1/mcp-oauth/*` (contracts/oauth.md SS5/SS9, tasks.md T015/
T015b). `_StrictModel` propia -- no se reusa `accounts.presentation.
payloads.StrictModel` para no crear una dependencia `mcp_oauth -> accounts`
que plan.md no autoriza (el unico contrato entre contextos es el que ya
describe: `mcp_oauth -> mcp` via `CallerScope`, `mcp_oauth -> iam` via
`current_owner`)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _StrictModel(BaseModel):
    """Rechaza campos no declarados (threat-model.md C-11, mismo criterio
    que el resto del panel)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopeDescriptor(_StrictModel):
    name: str
    label: str


class ConsentDetailResponse(_StrictModel):
    txn_id: uuid.UUID
    client_id: str
    client_name: str
    redirect_host: str
    scopes: list[ScopeDescriptor]
    expires_at: datetime


class RedirectResponse(_StrictModel):
    redirect_to: str


class GrantSummaryResponse(_StrictModel):
    grant_id: uuid.UUID
    client_id: str
    client_name: str
    redirect_host: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class GrantsListResponse(_StrictModel):
    grants: list[GrantSummaryResponse]
