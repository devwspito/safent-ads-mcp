"""Owner control plane plus bearer-only, business-bound local bridge transport."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.opportunities.domain.campaign_draft import DraftError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.runtime.connections import RuntimeConnections
from safent_ads.runtime.contracts import RuntimeResult
from safent_ads.runtime.pairing import PairingPoll, PairingRequest, RuntimePairings
from safent_ads.runtime.store import RuntimeJobStore, runtime_error

BusinessDep = Annotated[str, Depends(require_business_access)]
OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    runtime: Literal["codex", "claude"]


class LeaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    lease_token: str = Field(min_length=32, max_length=100)


class HeartbeatBody(LeaseBody):
    message: str = Field(min_length=1, max_length=2000)


class ReportBody(LeaseBody):
    result: RuntimeResult


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(default="", max_length=4000)


def build_runtime_router(jobs: RuntimeJobStore, connections: RuntimeConnections) -> APIRouter:
    router = APIRouter(tags=["runtime"])
    pairings = RuntimePairings(connections)

    @router.post("/api/v1/runtime/pair")
    async def pair(
        business_id: BusinessDep, owner: OwnerDep, body: PairingRequest, response: Response
    ) -> object:
        del owner
        response.headers["Cache-Control"] = "no-store"
        return await pairings.approve(business_id, body)

    @router.post("/runtime/v1/pair/poll")
    async def poll_pair(body: PairingPoll, response: Response) -> object:
        # No cookie authority, no writes: possession proof returns RSA ciphertext only.
        response.headers["Cache-Control"] = "no-store"
        return await pairings.poll(body.verifier)

    @router.get("/api/v1/runtime/connections")
    async def list_connections(business_id: BusinessDep) -> object:
        return await connections.list(business_id)

    @router.post("/api/v1/runtime/connections")
    async def connect(
        business_id: BusinessDep, owner: OwnerDep, body: ConnectBody, response: Response
    ) -> object:
        del owner  # authenticated owner is mandatory; the bridge never inherits owner rights
        response.headers["Cache-Control"] = "no-store"
        return await connections.create(business_id, body.label, body.runtime)

    @router.delete("/api/v1/runtime/connections/{identifier}")
    async def revoke(identifier: UUID, business_id: BusinessDep, owner: OwnerDep) -> object:
        del owner
        await connections.revoke(business_id, str(identifier))
        return {"revoked": True}

    @router.get("/api/v1/runtime/jobs")
    async def list_jobs(business_id: BusinessDep) -> object:
        return await jobs.list(business_id)

    @router.post("/api/v1/runtime/jobs/{identifier}/{action}")
    async def control(
        identifier: UUID,
        action: Literal["retry", "cancel"],
        business_id: BusinessDep,
        owner: OwnerDep,
        body: ControlBody,
    ) -> object:
        del owner
        if action == "retry":
            return await jobs.retry(business_id, str(identifier), body.message)
        return await jobs.cancel(business_id, str(identifier))

    async def identity(request: Request, *, touch: bool = True) -> tuple[str, str]:
        # No cookie fallback: CSRF exemption is safe only for this bearer-only surface.
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise runtime_error("RUNTIME_CREDENTIAL_REQUIRED", 401)
        return await connections.authenticate(header[7:], touch=touch)

    @router.post("/runtime/v1/claim")
    async def claim(request: Request) -> object:
        business, holder = await identity(request)
        return await jobs.claim(business, holder)

    @router.post("/runtime/v1/ping")
    async def ping(request: Request) -> object:
        business, holder = await identity(request, touch=False)
        return {"business_id": business, "connection_id": holder.removeprefix("bridge:")}

    @router.post("/runtime/v1/heartbeat")
    async def heartbeat(request: Request, body: HeartbeatBody) -> object:
        business, holder = await identity(request)
        return await jobs.heartbeat(
            business, str(body.job_id), holder, body.lease_token, body.message
        )

    @router.post("/runtime/v1/report")
    async def report(request: Request, body: ReportBody) -> object:
        business, holder = await identity(request)
        try:
            return await jobs.report(
                business, str(body.job_id), holder, body.lease_token, body.result
            )
        except DraftError as exc:
            raise runtime_error(exc.code) from exc

    return router
