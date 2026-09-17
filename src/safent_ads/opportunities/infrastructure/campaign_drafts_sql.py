"""One scoped transaction for each draft write, including promotion to a proposal."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.opportunities.application.errors import (
    AmbiguousActiveAccountForPlatformError,
    ChannelTypeNotEnabledError,
    DailyBudgetExceedsCapError,
    NoActiveAccountForPlatformError,
    OfferingNotFoundError,
)
from safent_ads.opportunities.application.propose_campaign import (
    ProposeCampaign,
    ProposeCampaignRequest,
)
from safent_ads.opportunities.domain.campaign_draft import (
    DraftError,
    DraftFields,
    completed_brief,
    missing_fields,
)
from safent_ads.opportunities.infrastructure.sql_repositories import (
    SqlAccountDailyCapPort,
    SqlActiveAccountLookupPort,
    SqlCampaignProposalPort,
    SqlOfferingExistsPort,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.proposal import ProposalInvariantError
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef

_LIST_LIMIT = 200


class CampaignDraftStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._sessions, self._clock = sessions, clock
        self._enabled_google_channels = enabled_google_channels

    async def save(
        self, business: str, key: str, revision: int | None, changes: DraftFields
    ) -> dict[str, Any]:
        async with self._sessions.begin() as session:
            params = {"business": UUID(business), "key": key}
            await self._business(session, business)
            # Serialize first creation and updates by a stable scoped key.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key,0))"),
                {"lock_key": business + ":campaign-draft:" + key},
            )
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM campaign_drafts WHERE business_id=:business "
                            "AND draft_key=:key FOR UPDATE"
                        ),
                        params,
                    )
                )
                .mappings()
                .one_or_none()
            )
            previous = {} if row is None else row["brief"]
            fields = DraftFields.model_validate(
                {**previous, **changes.model_dump(mode="json", exclude_unset=True)}
            )
            if not fields.title:
                raise DraftError("CAMPAIGN_DRAFT_TITLE_REQUIRED")
            await self._refs(session, business, fields)
            payload = fields.model_dump(mode="json")
            if row is not None:
                if payload == previous:
                    return self._view(row)
                if row["proposal_id"] is not None:
                    raise DraftError("CAMPAIGN_DRAFT_ALREADY_PROPOSED")
                if revision != row["revision"]:
                    raise DraftError("CAMPAIGN_DRAFT_CHANGED")
                sql = (
                    "UPDATE campaign_drafts SET brief=CAST(:brief AS jsonb),"
                    "revision=revision+1,updated_at=now() WHERE business_id=:business "
                    "AND draft_key=:key RETURNING *"
                )
            else:
                if revision is not None:
                    raise DraftError("CAMPAIGN_DRAFT_NOT_FOUND")
                sql = (
                    "INSERT INTO campaign_drafts(business_id,draft_key,brief) "
                    "VALUES(:business,:key,CAST(:brief AS jsonb)) RETURNING *"
                )
            result = (
                (await session.execute(text(sql), {**params, "brief": json.dumps(payload)}))
                .mappings()
                .one()
            )
            return self._view(result)

    async def list(self, business: str) -> dict[str, Any]:
        async with self._sessions() as session:
            await self._business(session, business)
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM campaign_drafts WHERE business_id=:business "
                            "ORDER BY updated_at DESC,id LIMIT 201"
                        ),
                        {"business": UUID(business)},
                    )
                )
                .mappings()
                .all()
            )
            return {
                "items": [self._view(row) for row in rows[:_LIST_LIMIT]],
                "has_more": len(rows) > _LIST_LIMIT,
            }

    async def get(self, business: str, draft_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            return self._view(await self._row(session, business, draft_id, lock=False))

    async def promote(
        self, business: str, draft_id: str, revision: int, *, proposed_by: str | None = None
    ) -> dict[str, Any]:
        async with self._sessions.begin() as session:
            row = await self._row(session, business, draft_id, lock=True)
            if row["proposal_id"] is not None:
                return self._view(row)
            if revision != row["revision"]:
                raise DraftError("CAMPAIGN_DRAFT_CHANGED")
            fields = DraftFields.model_validate(row["brief"])
            await self._refs(session, business, fields)
            brief = completed_brief(fields)
            if fields.account_ref is None:
                raise DraftError("CAMPAIGN_DRAFT_INCOMPLETE", ("account_ref",))
            request = ProposeCampaignRequest(
                business_id=BusinessId.parse(business),
                brief=brief,
                account_ref=EntityRef.parse(fields.account_ref),
                proposed_by=proposed_by,
            )
            # Existing proposal use case, same session: caps, exact account binding,
            # deduplication and human approval remain authoritative and atomic.
            service = ProposeCampaign(
                offerings=SqlOfferingExistsPort(session),
                accounts=SqlActiveAccountLookupPort(session),
                daily_caps=SqlAccountDailyCapPort(session),
                campaign_proposals=SqlCampaignProposalPort(session, clock=self._clock),
                clock=self._clock,
                enabled_google_channels=self._enabled_google_channels,
            )
            try:
                result = await service.execute(request)
            except (
                DailyBudgetExceedsCapError,
                NoActiveAccountForPlatformError,
                OfferingNotFoundError,
                AmbiguousActiveAccountForPlatformError,
            ) as exc:
                raise DraftError("CAMPAIGN_DRAFT_ACCOUNT_OR_LIMIT_BLOCKED") from exc
            except ChannelTypeNotEnabledError as exc:
                raise DraftError("CHANNEL_TYPE_NOT_ENABLED", (exc.channel,)) from exc
            except ProposalInvariantError as exc:
                raise DraftError("CAMPAIGN_DRAFT_PROPOSAL_CONFLICT") from exc
            if result.estado not in {"pending", "postponed"}:
                raise DraftError("CAMPAIGN_DRAFT_PROPOSAL_CONFLICT")
            result_row = (
                (
                    await session.execute(
                        text(
                            "UPDATE campaign_drafts SET proposal_id=:proposal,"
                            "revision=revision+1,updated_at=now() "
                            "WHERE id=:id AND business_id=:business RETURNING *"
                        ),
                        {
                            "proposal": UUID(result.proposal_id),
                            "id": UUID(draft_id),
                            "business": UUID(business),
                        },
                    )
                )
                .mappings()
                .one()
            )
            return self._view(result_row)

    @staticmethod
    async def _row(session: AsyncSession, business: str, draft_id: str, *, lock: bool) -> Any:
        statement = "SELECT * FROM campaign_drafts WHERE id=:id AND business_id=:business"
        if lock:
            statement += " FOR UPDATE"
        row = (
            (
                await session.execute(
                    text(statement), {"id": UUID(draft_id), "business": UUID(business)}
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DraftError("CAMPAIGN_DRAFT_NOT_FOUND")
        return row

    @staticmethod
    async def _business(session: AsyncSession, business: str) -> None:
        if (
            await session.execute(
                text("SELECT id FROM businesses WHERE id=:id FOR KEY SHARE"), {"id": UUID(business)}
            )
        ).scalar_one_or_none() is None:
            raise DraftError("CAMPAIGN_DRAFT_NOT_FOUND")

    @staticmethod
    async def _refs(session: AsyncSession, business: str, fields: DraftFields) -> None:
        if fields.offering_id is not None and not await SqlOfferingExistsPort(session).exists(
            business_id=BusinessId.parse(business), offering_id=fields.offering_id
        ):
            raise DraftError("CAMPAIGN_DRAFT_REFERENCE_NOT_FOUND")
        if fields.account_ref is not None:
            ref = EntityRef.parse(fields.account_ref)
            if (
                ref.business_id != UUID(business)
                or ref.connection_id is None
                or ref.level != EntityLevel.ACCOUNT
                or fields.platform is not None
                and ref.platform.value != fields.platform
            ):
                raise DraftError("CAMPAIGN_DRAFT_REFERENCE_NOT_FOUND")
            found = (
                await session.execute(
                    text(
                        "SELECT id FROM platform_accounts WHERE business_id=:business "
                        "AND account_ref=:ref"
                    ),
                    {"business": UUID(business), "ref": fields.account_ref},
                )
            ).scalar_one_or_none()
            if found is None:
                raise DraftError("CAMPAIGN_DRAFT_REFERENCE_NOT_FOUND")

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        fields = DraftFields.model_validate(row["brief"])
        return {
            "draft_id": str(row["id"]),
            "draft_key": row["draft_key"],
            "business_id": str(row["business_id"]),
            "revision": row["revision"],
            "state": "proposed" if row["proposal_id"] else "draft",
            "brief": fields.model_dump(mode="json"),
            "missing_fields": list(missing_fields(fields)),
            "proposal_id": str(row["proposal_id"]) if row["proposal_id"] else None,
            "executable": False,
            "updated_at": row["updated_at"].isoformat(),
        }
