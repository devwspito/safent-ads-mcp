"""`ApproveCampaignPackage` (tasks.md T022)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailSet,
    ScopeKind,
)
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.application.errors import (
    ChannelTypeNotEnabledError,
    PackageBrakeEngagedError,
    PackageChangedError,
    PackageExpiredError,
    PackageGuardrailBlockedError,
    PackageNotFoundError,
    PackageNotProposedError,
)
from safent_ads.packages.domain.campaign_package import PackageState
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.proposals.domain.authorization import AuthorizationDecision, SubjectKind
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock

from ..domain.conftest import (
    google_campaign,
    performance_max_ad_set,
    performance_max_campaign_native,
    propose_google_package,
    propose_meta_package,
)
from .conftest import (
    FakeCampaignPackageRepository,
    FakePackageAuthorizationRepository,
    FakePackagePublicationRepository,
)

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def _wide_open_guardrails(scope_ref: str) -> GuardrailSet:
    return GuardrailSet(
        scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=scope_ref),
        daily_cap=Money.of("1000"),
        monthly_cap=Money.of("10000"),
        floor=Money.of("1"),
        ceiling=Money.of("500"),
        max_step_pct=1.0,
        max_changes_per_entity_day=100,
    )


class Fixture:
    def __init__(self) -> None:
        self.package = propose_meta_package(now=NOW, ttl_hours=72)
        self.repo = FakeCampaignPackageRepository()
        self.repo.by_id[str(self.package.package_id)] = self.package
        self.publications = FakePackagePublicationRepository()
        self.authorizations = FakePackageAuthorizationRepository()
        self.brakes = FakeBrakeStatePort()
        self.guardrail_sets = FakeGuardrailSetRepository(
            {str(self.package.account_ref): _wide_open_guardrails(str(self.package.account_ref))}
        )
        self.spend_ledger = FakeSpendLedger()
        self.signer = FakeSignerPort()
        self.clock = FixedClock(NOW)

    def use_case(
        self,
        *,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> ApproveCampaignPackage:
        return ApproveCampaignPackage(
            packages=self.repo,
            publications=self.publications,
            authorizations=self.authorizations,
            brakes=self.brakes,
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=self.guardrail_sets,
            spend_ledger=self.spend_ledger,
            signer=self.signer,
            clock=self.clock,
            enabled_google_channels=enabled_google_channels,
        )

    def command(self, **overrides: object) -> ApproveCampaignPackageCommand:
        defaults: dict[str, object] = {
            "business_id": self.package.business_id,
            "package_id": self.package.package_id,
            "package_hash": self.package.package_hash.value,
            "approved_by": "owner-1",
        }
        defaults.update(overrides)
        return ApproveCampaignPackageCommand(**defaults)  # type: ignore[arg-type]


class TestHappyPath:
    async def test_approving_signs_one_human_authorization_with_package_subject(self) -> None:
        fixture = Fixture()

        result = await fixture.use_case().execute(fixture.command())

        assert result.grace_seconds == 45
        assert len(fixture.authorizations.saved) == 1
        authorization = fixture.authorizations.saved[0]
        assert authorization.proposal_id is None
        assert authorization.subject is not None
        assert authorization.subject.kind is SubjectKind.PACKAGE
        assert authorization.subject.id == str(fixture.package.package_id)
        assert authorization.decision is AuthorizationDecision.APPROVED

    async def test_approving_creates_a_pending_publication_with_the_signed_envelope(self) -> None:
        fixture = Fixture()

        result = await fixture.use_case().execute(fixture.command())

        record = fixture.publications.by_package_id[str(fixture.package.package_id)]
        assert record.publication_id == result.publication_id
        assert record.state == "pending"
        assert record.cursor == 0
        assert record.envelope.publication_id == result.publication_id

    async def test_package_transitions_to_approved(self) -> None:
        fixture = Fixture()

        await fixture.use_case().execute(fixture.command())

        assert fixture.package.state is PackageState.APPROVED


class TestIdempotentApproval:
    """ME-6 (revision de codigo, `contracts/api.md §R2.C`): idempotente por
    `(package_id, package_hash)` -- doble clic en «Aprobar y publicar» no
    gasta dos veces ni crea una segunda publicacion."""

    async def test_second_call_with_the_same_hash_returns_the_same_result(self) -> None:
        fixture = Fixture()

        first = await fixture.use_case().execute(fixture.command())
        second = await fixture.use_case().execute(fixture.command())

        assert second.publication_id == first.publication_id
        assert second.authorization_id == first.authorization_id
        assert second.approval_expires_at == first.approval_expires_at
        assert len(fixture.authorizations.saved) == 1
        assert len(fixture.publications.by_package_id) == 1


class TestTypedErrors:
    async def test_package_not_found(self) -> None:
        fixture = Fixture()

        with pytest.raises(PackageNotFoundError):
            await fixture.use_case().execute(fixture.command(package_id=PackageId.new()))

    async def test_package_not_proposed_when_already_approved_with_a_different_hash(self) -> None:
        """ME-6 (revision de codigo): con la MISMA huella, una segunda
        llamada es idempotente (ver `TestIdempotentApproval`) -- esto solo
        prueba que un `package_hash` DISTINTO tras aprobar sigue siendo un
        error, nunca una repeticion silenciosa."""
        fixture = Fixture()
        await fixture.use_case().execute(fixture.command())

        with pytest.raises(PackageNotProposedError):
            await fixture.use_case().execute(fixture.command(package_hash="f" * 64))

    async def test_package_not_proposed_when_invalidated_with_the_same_hash(self) -> None:
        # M4 (repaso de seguridad 0.2.23): `_replay_if_already_approved`
        # devolvia un 200 idempotente para CUALQUIER estado no-PROPOSED con
        # la MISMA huella -- una publicacion cancelada (`invalidate()`, FR-08)
        # se podia "reaprobar" en silencio via el replay en vez de un 409.
        # La huella idempotente solo aplica mientras el paquete SIGUE
        # `APPROVED`.
        fixture = Fixture()
        await fixture.use_case().execute(fixture.command())
        fixture.package.invalidate("cancelled_by_owner", NOW)

        with pytest.raises(PackageNotProposedError):
            await fixture.use_case().execute(fixture.command())

    async def test_package_changed_on_hash_mismatch(self) -> None:
        fixture = Fixture()

        with pytest.raises(PackageChangedError):
            await fixture.use_case().execute(fixture.command(package_hash="f" * 64))

    async def test_package_expired(self) -> None:
        fixture = Fixture()
        fixture.package = propose_meta_package(now=NOW, ttl_hours=1)
        fixture.repo.by_id[str(fixture.package.package_id)] = fixture.package
        fixture.clock = FixedClock(NOW + timedelta(hours=2))

        with pytest.raises(PackageExpiredError):
            await fixture.use_case().execute(fixture.command())

    async def test_brake_engaged_denies_approval(self) -> None:
        fixture = Fixture()
        account_ref = str(fixture.package.account_ref)
        scope = BrakeScope(kind=BrakeScopeKind.PLATFORM_ACCOUNT, ref=account_ref)
        await fixture.brakes.save(EmergencyBrake(scope=scope, mode=BrakeMode.ALL, engaged=True))

        with pytest.raises(PackageBrakeEngagedError):
            await fixture.use_case().execute(fixture.command())

    async def test_guardrail_blocked_when_budget_outside_limits(self) -> None:
        fixture = Fixture()
        account_ref = str(fixture.package.account_ref)
        fixture.guardrail_sets = FakeGuardrailSetRepository(
            {
                account_ref: GuardrailSet(
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=account_ref),
                    daily_cap=Money.of("1000"),
                    monthly_cap=Money.of("10000"),
                    floor=Money.of("1"),
                    ceiling=Money.of("5"),
                    max_step_pct=1.0,
                    max_changes_per_entity_day=100,
                )
            }
        )

        with pytest.raises(PackageGuardrailBlockedError):
            await fixture.use_case().execute(fixture.command())


def _pmax_fixture() -> Fixture:
    fixture = Fixture()
    fixture.package = propose_google_package(
        campaign=google_campaign(native=performance_max_campaign_native()),
        ad_sets=(performance_max_ad_set(),),
        now=NOW,
        ttl_hours=72,
    )
    fixture.repo.by_id[str(fixture.package.package_id)] = fixture.package
    fixture.guardrail_sets = FakeGuardrailSetRepository(
        {str(fixture.package.account_ref): _wide_open_guardrails(str(fixture.package.account_ref))}
    )
    return fixture


class TestChannelNotEnabled:
    """T035 security re-check (2026-09-15, CWE-284): defence in depth --
    `ProposeCampaignPackage` already rejects a disabled channel at propose
    time, but `ADS_GOOGLE_CHANNELS_ENABLED` can narrow between proposing and
    approving a package (a package proposed before the setting was
    narrowed)."""

    async def test_a_performance_max_package_is_denied_under_the_search_only_default(self) -> None:
        fixture = _pmax_fixture()

        with pytest.raises(ChannelTypeNotEnabledError) as excinfo:
            await fixture.use_case().execute(fixture.command())

        assert excinfo.value.channel == "PERFORMANCE_MAX"
        assert fixture.package.state is PackageState.PROPOSED  # nunca se aprueba
        assert len(fixture.authorizations.saved) == 0
        assert len(fixture.publications.by_package_id) == 0

    async def test_a_performance_max_package_approves_once_the_channel_is_enabled(self) -> None:
        fixture = _pmax_fixture()

        result = await fixture.use_case(
            enabled_google_channels=frozenset(
                {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
            )
        ).execute(fixture.command())

        assert result.publication_id
        assert fixture.package.state is PackageState.APPROVED
