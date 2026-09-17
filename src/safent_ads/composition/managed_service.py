"""Request-local central surface over the existing Ads SQL/use-case graph."""

from dataclasses import asdict
from typing import Any, Literal
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import Field, model_validator
from sqlalchemy import text

from safent_ads.composition.container import Container
from safent_ads.composition.mcp_write_adapter import ContainerProposalWriteAdapter
from safent_ads.iam.application.managed_ads_authority import ManagedAdsAdmission, ManagedAdsDenied
from safent_ads.iam.application.managed_token_locator import grant_locator, human_locator
from safent_ads.mcp.application.dto import Window
from safent_ads.mcp.infrastructure.sql_entity_read_port import SqlEntityReadPort
from safent_ads.mcp.presentation.ad_child_args import ChildPlanArgs
from safent_ads.mcp.presentation.args import (
    CauseArgs,
    EntityRefStr,
    ProposeBudgetChangeArgs,
    ProposePauseArgs,
    ToolArgs,
    WindowArgs,
    reject_urls_except_child_plan,
)
from safent_ads.proposals.application.prepare_managed_approval import PrepareManagedApproval
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.diff_hash import to_jsonable
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository


class EmptyArgs(ToolArgs):
    pass


class CampaignsArgs(EmptyArgs):
    limit: int = Field(default=50, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=1200)


class MetricsArgs(EmptyArgs):
    entity_ref: str = Field(min_length=1, max_length=1200)
    window: WindowArgs
    granularity: Literal["daily", "hourly"] = "daily"


class ProposalArgs(EmptyArgs):
    proposal_id: UUID


class ManagedAdChildArgs(ToolArgs):
    entity_ref: EntityRefStr
    child_plan: ChildPlanArgs
    cause: CauseArgs

    @model_validator(mode="before")
    @classmethod
    def _reject_urls_in_raw_strings(cls, data: Any) -> Any:  # noqa: ANN401
        # Mismo criterio que `ProposeAdChildArgs` (args.py): `child_plan.native`
        # de un anuncio Google lleva `final_url`/`final_urls` `https` a proposito
        # (M-3); `ad_child_creation.py` ya valida esas URLs con precision.
        return reject_urls_except_child_plan(data)


TOOL_MODELS: dict[str, type[ToolArgs]] = {
    "list_platform_accounts": EmptyArgs,
    "list_campaigns": CampaignsArgs,
    "get_entity_metrics": MetricsArgs,
    "propose_budget_change": ProposeBudgetChangeArgs,
    "propose_pause": ProposePauseArgs,
    "propose_ad_child": ManagedAdChildArgs,
    "get_proposal": ProposalArgs,
    "get_approval_review": ProposalArgs,
}


class ManagedAdsService:
    def __init__(self, container: Container):
        if not container.settings.managed_central or container.managed_human_authority is None:
            raise ValueError("managed central unavailable")
        self.container = container
        self.authority = container.managed_human_authority

    async def admit(
        self, token: str, operation: Literal["read", "propose", "approve", "execute"]
    ) -> ManagedAdsAdmission:
        locator = grant_locator(token)  # Untrusted hints only; never returned as principal.
        return await self.authority.introspect(
            token,
            expected_instance_id=locator.instance_id,
            account=locator.provider_account,
            operation=operation,
        )

    async def call(self, token: str, name: str, raw: dict[str, Any]) -> object:  # noqa: PLR0911 - closed operations
        model = TOOL_MODELS.get(name)
        if model is None:
            raise ManagedAdsDenied("managed_tool_not_allowed")
        args = model.model_validate(raw)
        operation: Literal["read", "propose", "approve", "execute"] = (
            "propose"
            if name.startswith("propose_")
            else "approve"
            if name == "get_approval_review"
            else "read"
        )
        admission = await self.admit(token, operation)
        binding = admission.binding
        business = str(binding.account.business_id)
        if isinstance(args, ManagedAdChildArgs):
            writer = ContainerProposalWriteAdapter(self.container, managed_binding=binding)
            return await writer.propose_ad_child(
                business_id=business,
                entity_ref=args.entity_ref,
                child_plan=args.child_plan.model_dump(mode="json"),
                cause_text=args.cause.text,
            )
        if isinstance(args, (ProposeBudgetChangeArgs, ProposePauseArgs)):
            if args.business_id != business:
                raise ManagedAdsDenied("managed_scope_mismatch")
            writer = ContainerProposalWriteAdapter(self.container, managed_binding=binding)
            evidence = tuple(
                (e.metric, e.actual, e.target, e.window_preset.value) for e in args.evidence
            )
            if isinstance(args, ProposeBudgetChangeArgs):
                return await writer.propose_budget_change(
                    business_id=business,
                    entity_ref=args.entity_ref,
                    cause_text=args.cause.text,
                    cause_signal_id=args.cause.signal_id,
                    cause_rule_id=args.cause.rule_id,
                    urgency=args.urgency.value,
                    evidence=evidence,
                    new_daily_budget_amount=args.new_daily_budget_amount,
                    new_daily_budget_currency=args.new_daily_budget_currency,
                )
            return await writer.propose_pause(
                business_id=business,
                entity_ref=args.entity_ref,
                cause_text=args.cause.text,
                cause_signal_id=args.cause.signal_id,
                cause_rule_id=args.cause.rule_id,
                urgency=args.urgency.value,
                evidence=evidence,
            )
        reads = SqlEntityReadPort(
            self.container.session_factory,
            self.container.clock,
            account_scope=binding.provider_account,
        )
        if isinstance(args, CampaignsArgs):
            return await reads.list_campaigns(
                business,
                platform=binding.account.platform.value,
                status=None,
                limit=args.limit,
                cursor=args.cursor,
            )
        if isinstance(args, MetricsArgs):
            window = Window(
                args.window.preset, args.window.lag_days, args.window.date_from, args.window.date_to
            )
            return await reads.get_entity_metrics(
                business, args.entity_ref, window=window, granularity=args.granularity
            )
        async with self.container.session_factory() as session:
            if isinstance(args, ProposalArgs):
                proposal_id = ProposalId(args.proposal_id)
                proposals = SqlProposalRepository(session)
                proposal = await proposals.get(proposal_id)
                if proposal is None or proposal.diff.managed_binding != binding:
                    raise ManagedAdsDenied("managed_proposal_unavailable")
                if name == "get_approval_review":
                    intent = await PrepareManagedApproval(
                        proposals, self.authority, self.container.clock
                    ).execute(proposal_id, admission)
                    return {
                        "intent_id": str(intent.intent_id),
                        "proposal_id": str(proposal_id),
                        "expires_at": intent.expires_at,
                        "review_url": self.container.settings.enterprise_origin.rstrip("/")
                        + "/#/ads/approve/"
                        + str(intent.intent_id),
                    }
                return {
                    "proposal_id": str(proposal_id),
                    "state": proposal.state.value,
                    "diff_hash": proposal.diff.diff_hash,
                    "diff": {
                        "entity_ref": str(proposal.diff.entity_ref),
                        "parameter": proposal.diff.parameter,
                        "before": to_jsonable(proposal.diff.before),
                        "after": to_jsonable(proposal.diff.after),
                    },
                }
            row = (
                (
                    await session.execute(
                        text("""SELECT account_ref,platform,external_account_id,currency,status
                FROM platform_accounts WHERE account_ref=:account
                    AND business_id=:business AND status='ACTIVE'"""),
                        {
                            "account": str(binding.provider_account),
                            "business": binding.account.business_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ManagedAdsDenied("managed_account_unavailable")
            return {"accounts": [dict(row)]}

    async def approve(self, assertion: str) -> dict[str, object]:
        proposal_id, diff_hash = human_locator(assertion)
        async with self.container.session_factory() as session:
            cases = self.container.build_execution_use_cases(session)
            result = await cases.submit_approval.execute(
                SubmitApprovalCommand(
                    ProposalId(proposal_id),
                    diff_hash,
                    "",
                    AuthorizationChannel.PANEL,
                    human_assertion=assertion,
                )
            )
            await session.commit()
            return {
                "status": "scheduled",
                "proposal_id": str(proposal_id),
                **jsonable_encoder(asdict(result)),
            }
