"""`ProposeCreative`: gestiona la puerta `POLICY_CHECK_REQUIRED`
(rest-api.md: "422 si verdict != PASS", mas estricto que
`CreativeAsset.propose()`, que solo exige "no FAIL"), la comprobacion de
que `extra_asset_ids` pertenezcan al MISMO negocio (IDOR) y que un fallo
del gateway (p.ej. `ad_set_ref` desconocido) no deje el activo marcado
`PROPOSED` sin que exista de verdad una `Proposal`."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from safent_ads.creative.application.errors import (
    AdSetReferenceNotFoundError,
    CreativeAssetNotFoundError,
    CreativeAssetPolicyCheckRequiredError,
)
from safent_ads.creative.application.ports import CreativePublicationProposal, ProposedAdCopy
from safent_ads.creative.application.propose_creative import ProposeCreative, ProposeCreativeCommand
from safent_ads.creative.domain.creative_asset import (
    CreativeAsset,
    InvalidCreativeAssetTransitionError,
)
from safent_ads.creative.domain.enums import (
    CreativeAssetState,
    MediaKind,
    PolicySeverity,
    PolicyVerdictResult,
)
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.policy import PolicyFinding, PolicyVerdict
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
)
from safent_ads.shared.ids import BusinessId
from tests.unit.creative.infrastructure.fakes import make_creative_asset


class _UpdateSpyAssetRepository:
    def __init__(self, delegate: InMemoryCreativeAssetRepository) -> None:
        self._delegate = delegate
        self.update_calls = 0

    async def get(self, asset_id: AssetId) -> CreativeAsset | None:
        return await self._delegate.get(asset_id)

    async def add(self, asset: CreativeAsset) -> None:
        await self._delegate.add(asset)

    async def update(self, asset: CreativeAsset) -> None:
        self.update_calls += 1
        await self._delegate.update(asset)

    async def list_for_business(
        self, business_id: BusinessId, *, media_kind: MediaKind | None = None
    ) -> tuple[CreativeAsset, ...]:
        return await self._delegate.list_for_business(business_id, media_kind=media_kind)


_PASS = PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=())
_WARN = PolicyVerdict(
    verdict=PolicyVerdictResult.WARN,
    findings=(PolicyFinding(code="CAPS_ABUSE", severity=PolicySeverity.WARN, human_message="x"),),
)
_FAIL = PolicyVerdict(
    verdict=PolicyVerdictResult.FAIL,
    findings=(PolicyFinding(code="X", severity=PolicySeverity.FAIL, human_message="x"),),
)

_RESULT = CreativePublicationProposal(
    proposal_id="prop-1", diff_hash="a" * 64, expires_at=datetime(2026, 1, 1, tzinfo=UTC)
)


class _FakeGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def propose_creative_publication(
        self,
        *,
        asset_id: AssetId,
        business_id: BusinessId,
        ad_set_ref: str,
        ad_copy: ProposedAdCopy,
        extra_asset_ids: Sequence[AssetId],
    ) -> CreativePublicationProposal:
        del ad_set_ref, ad_copy, extra_asset_ids
        if self._error is not None:
            raise self._error
        self.calls.append({"asset_id": asset_id, "business_id": business_id})
        return _RESULT


def _command(asset_id: AssetId, **overrides: object) -> ProposeCreativeCommand:
    defaults: dict[str, object] = {
        "asset_id": asset_id,
        "ad_set_ref": "meta:ad_set:123",
        "ad_copy": ProposedAdCopy(headline="H", primary_text="T", cta="Más información"),
    }
    defaults.update(overrides)
    return ProposeCreativeCommand(**defaults)  # type: ignore[arg-type]


def test_execute_proposes_a_ready_and_passing_asset() -> None:
    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        asset = make_creative_asset()
        asset.mark_ready(_PASS)
        await assets.add(asset)
        gateway = _FakeGateway()
        use_case = ProposeCreative(gateway, assets)

        result = await use_case.execute(_command(asset.asset_id))

        assert result.proposal_id == "prop-1"
        assert len(gateway.calls) == 1
        reloaded = await assets.get(asset.asset_id)
        assert reloaded is not None
        assert reloaded.state == CreativeAssetState.PROPOSED

    asyncio.run(_run())


def test_execute_raises_not_found_for_unknown_asset() -> None:
    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        use_case = ProposeCreative(_FakeGateway(), assets)

        with pytest.raises(CreativeAssetNotFoundError):
            await use_case.execute(_command(AssetId.new()))

    asyncio.run(_run())


@pytest.mark.parametrize("verdict", [None, _WARN, _FAIL])
def test_execute_requires_a_passing_policy_verdict(verdict: PolicyVerdict | None) -> None:
    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        asset = make_creative_asset()
        if verdict is not None:
            asset.mark_ready(verdict)
        await assets.add(asset)
        use_case = ProposeCreative(_FakeGateway(), assets)

        with pytest.raises(CreativeAssetPolicyCheckRequiredError):
            await use_case.execute(_command(asset.asset_id))

    asyncio.run(_run())


def test_execute_rejects_extra_asset_ids_from_another_business() -> None:
    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        asset = make_creative_asset()
        asset.mark_ready(_PASS)
        await assets.add(asset)
        foreign_extra = make_creative_asset()  # negocio distinto por defecto
        await assets.add(foreign_extra)
        use_case = ProposeCreative(_FakeGateway(), assets)

        with pytest.raises(CreativeAssetNotFoundError):
            await use_case.execute(
                _command(asset.asset_id, extra_asset_ids=(foreign_extra.asset_id,))
            )

    asyncio.run(_run())


def test_execute_never_calls_update_when_gateway_fails() -> None:
    """El doble en memoria comparte referencia con el agregado mutado en
    proceso (`asset.propose()` ya cambio `_state` in-place antes de llamar
    al gateway): la garantia real -- no persistir `PROPOSED` sin una
    `Proposal` de verdad -- solo es observable comprobando que `update()`
    nunca se invoca, no releyendo el mismo objeto en memoria."""

    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        asset = make_creative_asset()
        asset.mark_ready(_PASS)
        await assets.add(asset)
        spy = _UpdateSpyAssetRepository(assets)
        gateway = _FakeGateway(error=AdSetReferenceNotFoundError("nope"))
        use_case = ProposeCreative(gateway, spy)

        with pytest.raises(AdSetReferenceNotFoundError):
            await use_case.execute(_command(asset.asset_id))

        assert spy.update_calls == 0

    asyncio.run(_run())


def test_execute_raises_invalid_transition_when_asset_already_proposed() -> None:
    async def _run() -> None:
        assets = InMemoryCreativeAssetRepository()
        asset = make_creative_asset()
        asset.mark_ready(_PASS)
        asset.propose()
        await assets.add(asset)
        use_case = ProposeCreative(_FakeGateway(), assets)

        with pytest.raises(InvalidCreativeAssetTransitionError):
            await use_case.execute(_command(asset.asset_id))

    asyncio.run(_run())
