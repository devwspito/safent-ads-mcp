"""Dobles en memoria de los 10 puertos de lectura (T045). Dos negocios de
muestra (`BUSINESS_A`/`BUSINESS_B`) para que los tests de aislamiento entre
negocios (`test_idor_sweep`-style) tengan algo real que cruzar: pedir el
`signal_id` de B con el `business_id` de A debe fallar con
`EntityNotFoundError`, nunca devolver el dato."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from safent_ads.mcp.application.dto import (
    AccountFreshness,
    AccountStatus,
    AnomalySummary,
    AutonomyLevel,
    BrandAssetSummary,
    BrandKitDetail,
    BusinessSummary,
    CalendarEventDetail,
    CalendarEventSummary,
    CampaignStatus,
    CampaignSummary,
    Cause,
    ColorSwatchDetail,
    CreativeBriefSummary,
    CreativeDetail,
    CreativeJobStatus,
    CreativeSummary,
    DecisionLogEntryDetail,
    DecisionLogEntrySummary,
    EntityLevel,
    EntitySummary,
    GaqlResult,
    Granularity,
    GuardrailInfo,
    InsightsSnapshot,
    KillSwitchStatus,
    MediaKind,
    MetricPoint,
    MetricsSeries,
    OfferingSummary,
    PacingInfo,
    Page,
    PlatformAccountSummary,
    PlatformCode,
    PortfolioOverview,
    ProposalDetail,
    ProposalState,
    ProposalSummary,
    ProposedDiff,
    RuleDetail,
    RuleExplanation,
    RuleSummary,
    SignalDetail,
    SignalKind,
    SignalsPage,
    SignalSummary,
    ToneOfVoiceDetail,
    TypographyDetail,
    Window,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.application.seat_authority import (
    SeatAdmission,
    SeatAuthorityDeniedError,
    SeatAuthorityUnavailableError,
)
from safent_ads.shared.read_models.dto import (
    Caps,
    CapsSource,
    Freshness,
    Money,
    Pacing,
    SignalOutcome,
    SignalOutcomeStatus,
    SpendBreakdown,
)

BUSINESS_A = "11111111-1111-1111-1111-111111111111"
BUSINESS_B = "22222222-2222-2222-2222-222222222222"

_CAMPAIGN_A = "google:campaign:1111111111"
_CAMPAIGN_B = "google:campaign:2222222222"
_SIGNAL_A = "sig-a-1"
_SIGNAL_B = "sig-b-1"
_RULE_A = "M05"
_PROPOSAL_A = "prop-a-1"


def _require_match(actual: str, expected: str, what: str) -> None:
    """Simula el `ENTITY_NOT_FOUND` que un adaptador real devolveria si el
    id pedido no pertenece al `business_id` de la llamada: mismo codigo
    tanto si no existe como si es de otro negocio, para no filtrar
    existencia (mismo principio que `contracts/rest-api.md`)."""
    if actual != expected:
        raise EntityNotFoundError(f"{what} no encontrado para este negocio")


class FakeBusinessDirectory:
    async def list_businesses(self, allowed_business_ids: frozenset[str]) -> list[BusinessSummary]:
        all_businesses = [
            BusinessSummary(BUSINESS_A, "Negocio Ejemplo", "Europe/Madrid", "EUR"),
            BusinessSummary(BUSINESS_B, "Negocio B", "Europe/Madrid", "EUR"),
        ]
        return [b for b in all_businesses if b.business_id in allowed_business_ids]


class FakePortfolioReadPort:
    async def list_platform_accounts(self, business_id: str) -> list[PlatformAccountSummary]:
        return [
            PlatformAccountSummary(
                f"google:account:{business_id[:8]}",
                PlatformCode.GOOGLE,
                "EUR",
                "Europe/Madrid",
                AccountStatus.ACTIVE,
                "standard",
            )
        ]

    async def get_portfolio_overview(
        self, _business_id: str, _window: Window
    ) -> PortfolioOverview:
        freshness = Freshness(datetime.now(UTC), 12, False)
        return PortfolioOverview(
            spend=SpendBreakdown(
                window=Money(Decimal("1200.50")),
                today=Money(Decimal("90.00")),
                mtd=Money(Decimal("3400.00")),
            ),
            caps=Caps(daily=Money(Decimal("150.00")), monthly=None, source=CapsSource.GUARDRAIL),
            pacing=Pacing(index_pct=1.1, projection_pct=78.0, days_remaining=9),
            conversions_by_kind={"lead": 40, "business_conversion": 6},
            cost_per_lead=Money(Decimal("30.01")),
            cost_per_business_conversion=Money(Decimal("200.08")),
            freshness=freshness,
            is_partial=False,
            degraded_accounts=[],
            top_movers=[],
        )

    async def get_data_freshness(self, business_id: str) -> list[AccountFreshness]:
        return [
            AccountFreshness(
                f"google:account:{business_id[:8]}", Freshness(datetime.now(UTC), 12, False)
            )
        ]


class FakeEntityReadPort:
    async def list_campaigns(
        self,
        business_id: str,
        *,
        platform: str | None,
        status: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[CampaignSummary]:
        del platform, status, limit, cursor
        entity_ref = _CAMPAIGN_A if business_id == BUSINESS_A else _CAMPAIGN_B
        return Page([self._campaign(entity_ref)], None)

    def _campaign(self, entity_ref: str) -> CampaignSummary:
        return CampaignSummary(
            entity_ref,
            "Búsqueda Marca",
            CampaignStatus.ACTIVE,
            Money(Decimal("120.00")),
            "learned",
            True,
            SignalKind.SELL,
        )

    async def get_campaign(self, business_id: str, entity_ref: str) -> CampaignSummary:
        owner = BUSINESS_A if entity_ref == _CAMPAIGN_A else BUSINESS_B
        _require_match(business_id, owner, entity_ref)
        return self._campaign(entity_ref)

    async def list_children(
        self, business_id: str, entity_ref: str, *, limit: int, cursor: str | None
    ) -> Page[EntitySummary]:
        owner = BUSINESS_A if entity_ref == _CAMPAIGN_A else BUSINESS_B
        _require_match(business_id, owner, entity_ref)
        del limit, cursor
        child = EntitySummary(
            f"{entity_ref}:child", "Anuncio 1", EntityLevel.AD, CampaignStatus.ACTIVE, True
        )
        return Page([child], None)

    async def list_creatives(
        self, business_id: str, *, media_kind: str | None, limit: int, cursor: str | None
    ) -> Page[CreativeSummary]:
        del media_kind, limit, cursor
        return Page([self._creative(business_id)], None)

    def _creative(self, business_id: str) -> CreativeSummary:
        return CreativeSummary(f"asset-{business_id[:8]}", MediaKind.IMAGE, "1200x628", [], "ok")

    async def get_creative(self, business_id: str, asset_id: str) -> CreativeDetail:
        expected = f"asset-{business_id[:8]}"
        _require_match(asset_id, expected, asset_id)
        return CreativeDetail(self._creative(business_id), signal_id=None)

    async def get_entity_metrics(
        self, business_id: str, entity_ref: str, *, window: Window, granularity: str
    ) -> MetricsSeries:
        del business_id, window
        point = MetricPoint(datetime.now(UTC), Money(Decimal("40.00")), 3, Money(Decimal("13.33")))
        return MetricsSeries(entity_ref, Granularity(granularity), [point])

    async def get_insights(
        self, business_id: str, entity_ref: str, *, window: Window, breakdown: str | None
    ) -> InsightsSnapshot:
        del business_id, window, breakdown
        return InsightsSnapshot(entity_ref, datetime.now(UTC), 900, {"ctr": 1.2})


class FakeGaqlPort:
    async def run_gaql(self, business_id: str, account_ref: str, query: str) -> GaqlResult:
        del business_id, query
        return GaqlResult(account_ref, [{"campaign.id": "1234567890"}], 1)


class FakeSignalReadPort:
    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> SignalsPage:
        del kind, min_strength, since, limit, cursor
        return SignalsPage(
            items=[self._signal(business_id)],
            cursor=None,
            confirmed_rate_pct=None,
            confirmed_rate_sample=3,
        )

    def _signal(self, business_id: str) -> SignalSummary:
        signal_id = _SIGNAL_A if business_id == BUSINESS_A else _SIGNAL_B
        entity_ref = _CAMPAIGN_A if business_id == BUSINESS_A else _CAMPAIGN_B
        return SignalSummary(
            signal_id,
            entity_ref,
            SignalKind.SELL,
            78,
            Cause("CPL 41 € vs 28 €", signal_id, _RULE_A),
            Money(Decimal("310.00")),
            "7D",
            SignalOutcome(
                status=SignalOutcomeStatus.IN_PROGRESS, days_remaining=6, evaluated_at=None
            ),
        )

    async def get_signal(self, business_id: str, signal_id: str) -> SignalDetail:
        owner = BUSINESS_A if signal_id == _SIGNAL_A else BUSINESS_B
        _require_match(business_id, owner, signal_id)
        return SignalDetail(self._signal(business_id))

    async def explain_signal(self, business_id: str, signal_id: str) -> SignalDetail:
        detail = await self.get_signal(business_id, signal_id)
        return SignalDetail(detail.summary, narrative="CPL por encima del objetivo en 7D.")

    async def list_anomalies(self, business_id: str, *, since: datetime) -> list[AnomalySummary]:
        del since
        entity_ref = _CAMPAIGN_A if business_id == BUSINESS_A else _CAMPAIGN_B
        return [AnomalySummary(f"anomaly-{business_id[:8]}", entity_ref, "ewma_drift", 3.2, "high")]

    async def get_pacing(self, business_id: str, entity_ref: str) -> PacingInfo:
        del business_id
        return PacingInfo(
            entity_ref, 1.1, Money(Decimal("100")), Money(Decimal("20")), Money(Decimal("95"))
        )


class FakeRuleReadPort:
    async def list_rules(
        self, business_id: str, *, platform: str | None, enabled: bool | None
    ) -> list[RuleSummary]:
        del business_id, platform, enabled
        return [RuleSummary(_RULE_A, "M05", PlatformCode.GOOGLE, True, AutonomyLevel.AUTO)]

    async def get_rule(self, business_id: str, rule_id: str) -> RuleDetail:
        del business_id
        _require_match(rule_id, _RULE_A, rule_id)
        summary = RuleSummary(rule_id, "M05", PlatformCode.GOOGLE, True, AutonomyLevel.AUTO)
        return RuleDetail(summary, "cpl > target*1.3", "3D", "lower_budget", 30.0, 24)

    async def explain_rule(
        self, business_id: str, rule_id: str, *, entity_ref: str | None
    ) -> RuleExplanation:
        del business_id, entity_ref
        return RuleExplanation(rule_id, True, "CPL 41 vs objetivo 28", {"budget": "120->84"})

    async def list_guardrails(self, business_id: str, scope_ref: str) -> list[GuardrailInfo]:
        del business_id
        return [
            GuardrailInfo(
                scope_ref,
                Money(Decimal("500")),
                Money(Decimal("5000")),
                Money(Decimal("20")),
                Money(Decimal("500")),
                30.0,
                2,
            )
        ]

    async def get_kill_switch_status(self, business_id: str) -> KillSwitchStatus:
        del business_id
        return KillSwitchStatus(False, "global", "ALL", None, None)


class FakeProposalReadPort:
    async def list_proposals(
        self,
        business_id: str,
        *,
        state: str | None,
        cause_key: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[ProposalSummary]:
        del state, cause_key, limit, cursor
        return Page([self._proposal(business_id)], None)

    def _proposal(self, business_id: str) -> ProposalSummary:
        entity_ref = _CAMPAIGN_A if business_id == BUSINESS_A else _CAMPAIGN_B
        diff = ProposedDiff("daily_budget", "90.00", "117.00", "a1b2c3d4")
        return ProposalSummary(
            _PROPOSAL_A,
            entity_ref,
            diff,
            "budget_increase",
            "normal",
            Money(Decimal("810.00")),
            datetime.now(UTC),
            ProposalState.PENDING,
        )

    async def get_proposal(self, business_id: str, proposal_id: str) -> ProposalDetail:
        _require_match(proposal_id, _PROPOSAL_A, proposal_id)
        return ProposalDetail(self._proposal(business_id), [], None, _RULE_A, [])


class FakeCatalogReadPort:
    async def list_offerings(self, business_id: str) -> list[OfferingSummary]:
        del business_id
        return [OfferingSummary("offering-1", "Búsqueda Marca")]

    async def list_calendar_events(
        self, business_id: str, *, open_only: bool
    ) -> list[CalendarEventSummary]:
        del business_id, open_only
        return [CalendarEventSummary("evt-1", "offering-1", "Lanzamiento 2026", "season", True)]

    async def get_calendar_event(
        self, business_id: str, calendar_event_id: str
    ) -> CalendarEventDetail:
        del business_id
        summary = CalendarEventSummary(
            calendar_event_id, "offering-1", "Lanzamiento 2026", "season", True
        )
        today = datetime.now(UTC).date()
        return CalendarEventDetail(summary, today, today, None, None)


class FakeAuditReadPort:
    async def search_decision_log(
        self,
        business_id: str,
        *,
        since: datetime,
        until: datetime,
        event_type: str | None,
        entity_ref: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[DecisionLogEntrySummary]:
        del since, until, event_type, entity_ref, limit, cursor
        entry = DecisionLogEntrySummary(1, datetime.now(UTC), "signal.evaluated", None, "h1")
        del business_id
        return Page([entry], None)

    async def get_decision_log_entry(self, business_id: str, seq: int) -> DecisionLogEntryDetail:
        del business_id
        summary = DecisionLogEntrySummary(seq, datetime.now(UTC), "signal.evaluated", None, "h1")
        return DecisionLogEntryDetail(summary, {}, "")


class FakeCreativeReadPort:
    async def list_creative_briefs(self, business_id: str) -> list[CreativeBriefSummary]:
        del business_id
        return [CreativeBriefSummary("brief-1", None, "draft")]

    async def get_creative_job(self, business_id: str, job_id: str) -> CreativeJobStatus:
        del business_id
        return CreativeJobStatus(job_id, "running", 0.4, None, [])


class FakeBrandReadPort:
    async def get_brand_kit(self, business_id: str) -> BrandKitDetail:
        return BrandKitDetail(
            brand_kit_id=f"brand-{business_id[:8]}",
            business_id=business_id,
            typography=TypographyDetail("Fake Sans", None, "Google Fonts, SIL OFL", ["regular"]),
            palette=[ColorSwatchDetail("primary", "#101010", 18.0, True)],
            tone_of_voice=ToneOfVoiceDetail("Cercano y claro.", ["cercano"], ["jerga"]),
            assets=[
                BrandAssetSummary(
                    f"logo-{business_id[:8]}", "logo_vector", "s3://logo.svg", "no deformar"
                )
            ],
            claims_allowlist=["clases en directo"],
            forbidden_claims=["garantizado", "100% aprobados", "plaza asegurada"],
            legal_disclaimers=[],
            platform_constraints=[],
            is_complete=True,
            updated_at=datetime.now(UTC),
        )

    async def list_brand_assets(
        self, business_id: str, *, kind: str | None
    ) -> list[BrandAssetSummary]:
        kit = await self.get_brand_kit(business_id)
        if kind is None:
            return kit.assets
        return [asset for asset in kit.assets if asset.kind == kind]


class FakeSeatAuthority:
    """Doble configurable de `SeatAuthorityPort` (A2, tasks.md): A3-A6
    avanzan sin Enterprise registrando admisiones o denegaciones por
    credencial. Una credencial no registrada se deniega (denegar por
    defecto, igual que la autoridad real)."""

    def __init__(self) -> None:
        self._admissions: dict[str, SeatAdmission] = {}
        self._unavailable: set[str] = set()

    def admit(self, credential: str, admission: SeatAdmission) -> None:
        self._admissions[credential] = admission

    def mark_unavailable(self, credential: str) -> None:
        self._unavailable.add(credential)

    async def resolve(self, credential: str) -> SeatAdmission:
        if credential in self._unavailable:
            raise SeatAuthorityUnavailableError("seat_authority_unavailable")
        admission = self._admissions.get(credential)
        if admission is None:
            raise SeatAuthorityDeniedError("seat_authority_denied")
        return admission


class FakeCapabilityReadPort:
    def __init__(self, configured_keys: frozenset[str] = frozenset()) -> None:
        self._configured_keys = configured_keys

    async def configured_byok_keys(self) -> frozenset[str]:
        return self._configured_keys
