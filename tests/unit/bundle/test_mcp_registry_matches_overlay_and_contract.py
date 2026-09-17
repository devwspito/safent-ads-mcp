"""B-1 (checklists/final-review.md): la guarda que faltaba. `test_bundle_
consistency.py::test_overlay_tools_match_contract_exactly` solo compara
documento contra documento (`policy_overlay.json` frente a `contracts/
mcp-tools.md`); nada comparaba ninguno de los dos contra el `ToolRegistry`
real, asi que 11 herramientas declaradas en el overlay nunca se sirvieron
(`ningun modulo de composition/ las importaba`) y 6 vivas no tenian
entrada en el overlay.

Este modulo construye el registro real -- mismos fakes que `tests/unit/
mcp/presentation/conftest.py` y los `*_services()` de sus tests hermanos
(`test_experiment_tools.py`, `test_opportunity_tools.py`,
`test_search_terms_tools.py`), mas los tres constructores nuevos de esta
rama (`economics_tools.py`/`optimization_tools.py`/
`creative_generation_tools.py`) -- y afirma que coinciden las tres fuentes:
`{d.name for d in registry} == set(overlay) == contract_tool_names()`.

Tambien cruza `catalog.py::_WRITE_CATALOG` contra `hermes.capabilities.
tool_sensitivity._SAFENT_ADS_WRITE_TOOLS` del lado del runtime (companion
checkout en un repo hermano, T091/024) -- en sentido inverso al que ya
hace `lumen-runtime-next/tests/unit/capabilities/
test_ads_companion_sensitivity_drift.py` (esa lee ESTE `catalog.py` y
comprueba que cada `_WRITE_CATALOG` clasifica SPEND; esta lee SU
`tool_sensitivity.py` y comprueba que cada `_WRITE_CATALOG` aparece en su
lista). Mismo alcance que esa guarda a proposito -- ni ella ni esta cubren
`propose_campaign`/`propose_experiment`/`propose_reallocation_plan`/
`generate_creative_assets`: son `ToolClass.PROPOSAL` pero viven en sus
propios modulos autonomos (T114/T201/B-1), nunca en `_WRITE_CATALOG`. Es
un hueco real y preexistente en la clasificacion SPEND del runtime --
reportado, no resuelto aqui: tocar `tool_sensitivity.py` vive en otro
repo, fuera de esta rama."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.renderer_selector import RendererSelector
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
    InMemoryCreativeBriefRepository,
    InMemoryCreativeJobRepository,
)
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryLagCurveRepository,
    InMemoryPlatformDivergenceRepository,
    InMemoryUnitEconomicsProfileRepository,
)
from safent_ads.mcp.application.dto import Window
from safent_ads.mcp.application.proposal_write_port import (
    DefensiveActionResult,
    ProposalWriteResult,
    WithdrawProposalResult,
)
from safent_ads.mcp.application.search_terms_ports import BudgetEnvelope, SearchTermsResult
from safent_ads.mcp.presentation.catalog import _WRITE_CATALOG, build_default_registry
from safent_ads.mcp.presentation.creative_generation_tools import CreativeGenerationToolServices
from safent_ads.mcp.presentation.experiment_tools import ExperimentToolServices
from safent_ads.mcp.presentation.native_ads_tools import NativeAdsToolServices
from safent_ads.mcp.presentation.opportunity_tools import OpportunityToolServices
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry
from safent_ads.mcp.presentation.search_terms_tools import SearchTermsToolServices
from safent_ads.mcp.testing.fakes import (
    FakeAuditReadPort,
    FakeBrandReadPort,
    FakeBusinessDirectory,
    FakeCapabilityReadPort,
    FakeCatalogReadPort,
    FakeCreativeReadPort,
    FakeEntityReadPort,
    FakeGaqlPort,
    FakePortfolioReadPort,
    FakeProposalReadPort,
    FakeRuleReadPort,
    FakeSignalReadPort,
)
from safent_ads.opportunities.application.list_opportunities import ListOpportunities
from safent_ads.opportunities.application.propose_campaign import ProposeCampaign
from safent_ads.opportunities.testing.in_memory_repositories import (
    InMemoryAccountDailyCapPort,
    InMemoryActiveAccountLookupPort,
    InMemoryCampaignProposalPort,
    InMemoryOfferingExistsPort,
)
from safent_ads.optimization.application.design_experiment import DesignExperiment
from safent_ads.optimization.application.get_calibration_report import GetCalibrationReport
from safent_ads.optimization.application.get_experiment_status import GetExperimentStatus
from safent_ads.optimization.application.propose_experiment import ProposeExperiment
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryCalibrationInputPort,
    InMemoryContributionMarginPort,
    InMemoryDiagnosisMetricsPort,
    InMemoryExperimentProposalPort,
    InMemoryExperimentRepository,
    InMemoryMarginalEstimateRepository,
    InMemoryReallocationCandidateRepository,
    InMemoryReallocationProposalPort,
    InMemoryResponseCurveRepository,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.bundle.test_bundle_consistency import contract_tool_names

_NOW = datetime(2026, 9, 10, tzinfo=UTC)

_RUNTIME_TOOL_SENSITIVITY = (
    Path(os.environ.get("LUMEN_RUNTIME_REPO", str(Path.home() / "Desktop" / "lumen-runtime-next")))
    / "src/hermes/capabilities/tool_sensitivity.py"
)


class _FakeProposalWritePort:
    """Nunca invocado: `build_default_registry` solo comprueba
    `write_port is not None` para decidir si registra `_WRITE_CATALOG` --
    los handlers se construyen (cierran sobre este objeto) pero esta guarda
    solo lee `{d.name for d in registry}`, nunca despacha una llamada."""

    async def propose_budget_change(self, **_kwargs: object) -> ProposalWriteResult:
        raise NotImplementedError

    async def propose_pause(self, **_kwargs: object) -> ProposalWriteResult:
        raise NotImplementedError

    async def propose_targeting_change(self, **_kwargs: object) -> ProposalWriteResult:
        raise NotImplementedError

    async def propose_creative_publication(self, **_kwargs: object) -> ProposalWriteResult:
        raise NotImplementedError

    async def withdraw_proposal(self, **_kwargs: object) -> WithdrawProposalResult:
        raise NotImplementedError

    async def apply_defensive_action(self, **_kwargs: object) -> DefensiveActionResult:
        raise NotImplementedError


class _FakeSearchTermReadPort:
    async def list_search_terms(
        self, business_id: str, account_ref: str, *, window: Window
    ) -> SearchTermsResult:
        del business_id, account_ref, window
        return SearchTermsResult(account_ref="google:1", is_supported=True, reason=None, terms=[])


class _FakeBudgetEnvelopeReadPort:
    async def get_budget_envelope(self, business_id: str) -> BudgetEnvelope:
        del business_id
        return BudgetEnvelope(
            monthly_cap_minor=None,
            spent_month_to_date_minor=None,
            headroom_minor=None,
            projected_month_end_minor=None,
            currency="EUR",
            as_of=_NOW,
            reason="no_platform_account",
        )


class _EmptyOpenOpportunityPort:
    async def list_open(self, *, business_id: object) -> tuple[object, ...]:
        del business_id
        return ()


def _read_model_ports() -> ReadModelPorts:
    return ReadModelPorts(
        business_directory=FakeBusinessDirectory(),
        portfolio=FakePortfolioReadPort(),
        entity=FakeEntityReadPort(),
        gaql=FakeGaqlPort(),
        signal=FakeSignalReadPort(),
        rule=FakeRuleReadPort(),
        proposal=FakeProposalReadPort(),
        catalog=FakeCatalogReadPort(),
        audit=FakeAuditReadPort(),
        creative=FakeCreativeReadPort(),
        brand=FakeBrandReadPort(),
        capability=FakeCapabilityReadPort(),
    )


def _experiment_services() -> ExperimentToolServices:
    experiments = InMemoryExperimentRepository()
    return ExperimentToolServices(
        design=DesignExperiment(),
        propose=ProposeExperiment(
            proposals=InMemoryExperimentProposalPort(experiments),
            experiments=experiments,
            clock=FixedClock(_NOW),
        ),
        status=GetExperimentStatus(experiments),
        calibration_report=GetCalibrationReport(InMemoryCalibrationInputPort()),
    )


def _search_terms_services() -> SearchTermsToolServices:
    return SearchTermsToolServices(
        search_terms=_FakeSearchTermReadPort(), budget_envelope=_FakeBudgetEnvelopeReadPort()
    )


def _opportunity_services() -> OpportunityToolServices:
    return OpportunityToolServices(
        propose_campaign=ProposeCampaign(
            offerings=InMemoryOfferingExistsPort(),
            accounts=InMemoryActiveAccountLookupPort(),
            daily_caps=InMemoryAccountDailyCapPort(),
            campaign_proposals=InMemoryCampaignProposalPort(),
            clock=FixedClock(_NOW),
        ),
        list_opportunities=ListOpportunities(opportunities=_EmptyOpenOpportunityPort()),
    )


def _economics_service() -> EconomicsQueryService:
    return EconomicsQueryService(
        profiles=InMemoryUnitEconomicsProfileRepository(),
        lag_curves=InMemoryLagCurveRepository(),
        divergences=InMemoryPlatformDivergenceRepository(),
        clock=FixedClock(_NOW),
    )


def _optimization_service() -> OptimizationQueryService:
    return OptimizationQueryService(
        marginal_estimates=InMemoryMarginalEstimateRepository(),
        diagnosis_metrics=InMemoryDiagnosisMetricsPort(),
        response_curves=InMemoryResponseCurveRepository(),
        contribution_margins=InMemoryContributionMarginPort(),
        reallocation_candidates=InMemoryReallocationCandidateRepository(),
        reallocation_proposals=InMemoryReallocationProposalPort(),
        clock=FixedClock(_NOW),
    )


def _creative_generation_services() -> CreativeGenerationToolServices:
    briefs = InMemoryCreativeBriefRepository()
    assets = InMemoryCreativeAssetRepository()
    jobs = InMemoryCreativeJobRepository()
    return CreativeGenerationToolServices(
        briefs=briefs,
        assets=assets,
        generate_creative_assets=GenerateCreativeAssets(
            briefs=briefs,
            jobs=jobs,
            assets=assets,
            image_renderers={},
            renderer_selector=RendererSelector(),
            gpu_lease=InProcessGpuQueue(FixedClock(_NOW)),
        ),
        run_policy_check=RunPolicyCheck(LocalPolicyChecker(assets), assets),
    )


def _build_real_registry() -> ToolRegistry:
    return build_default_registry(
        _read_model_ports(),
        FixedClock(_NOW),
        _FakeProposalWritePort(),
        experiment_services=_experiment_services(),
        search_terms_services=_search_terms_services(),
        opportunity_services=_opportunity_services(),
        economics_service=_economics_service(),
        optimization_service=_optimization_service(),
        creative_generation_services=_creative_generation_services(),
        native_ads_services=NativeAdsToolServices(AsyncMock()),
        offering_creation=AsyncMock(),
        campaign_drafts=AsyncMock(),
    )


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return _build_real_registry()


@pytest.fixture(scope="module")
def registry_tool_names(registry: ToolRegistry) -> set[str]:
    return {definition.name for definition in registry}


@pytest.fixture(scope="module")
def overlay_tool_names() -> set[str]:
    root = Path(__file__).resolve().parents[3]
    policy = json.loads((root / "safent-bundle/policy/policy_overlay.json").read_text())
    return set(policy["policy_overlay"])


class TestRegistryOverlayAndContractAgree:
    def test_registry_is_non_empty(self, registry_tool_names: set[str]) -> None:
        """Guarda el propio parser: un registro vacio haria las tres
        comparaciones de abajo vacuamente ciertas."""
        assert len(registry_tool_names) > 50

    def test_registry_matches_the_overlay(
        self, registry_tool_names: set[str], overlay_tool_names: set[str]
    ) -> None:
        assert registry_tool_names == overlay_tool_names, (
            f"en overlay, no en el registro: {overlay_tool_names - registry_tool_names}; "
            f"en el registro, no en overlay: {registry_tool_names - overlay_tool_names}"
        )

    def test_registry_matches_the_markdown_contract(self, registry_tool_names: set[str]) -> None:
        names = contract_tool_names()
        assert registry_tool_names == names, (
            f"en el contrato, no en el registro: {names - registry_tool_names}; "
            f"en el registro, no en el contrato: {registry_tool_names - names}"
        )

    def test_overlay_matches_the_markdown_contract(self, overlay_tool_names: set[str]) -> None:
        names = contract_tool_names()
        assert overlay_tool_names == names


class TestWriteCatalogMatchesRuntimeSpendClassification:
    """Sentido inverso de `lumen-runtime-next/tests/unit/capabilities/
    test_ads_companion_sensitivity_drift.py`: esa lee `catalog.py` desde el
    runtime; esta lee `tool_sensitivity.py` desde aqui. Cross-repo por
    diseno -- se salta en voz alta con el motivo cuando ese checkout no
    esta presente, igual que la guarda del runtime hace al reves."""

    @staticmethod
    @pytest.fixture(scope="class")
    def runtime_write_tools() -> frozenset[str]:
        if not _RUNTIME_TOOL_SENSITIVITY.is_file():
            pytest.skip(
                f"lumen-runtime-next no encontrado en {_RUNTIME_TOOL_SENSITIVITY} "
                "(fijar LUMEN_RUNTIME_REPO para otra ruta) -- se salta el cruce SPEND"
            )
        return _parse_frozenset_literal(
            _RUNTIME_TOOL_SENSITIVITY.read_text(), "_SAFENT_ADS_WRITE_TOOLS"
        )

    def test_write_catalog_is_non_empty(self) -> None:
        write_tool_names = {name for name, _description, _args_model in _WRITE_CATALOG}
        assert len(write_tool_names) > 0

    def test_every_write_catalog_tool_is_in_the_runtime_spend_list(
        self, runtime_write_tools: frozenset[str]
    ) -> None:
        write_tool_names = {name for name, _description, _args_model in _WRITE_CATALOG}
        missing = write_tool_names - runtime_write_tools
        assert not missing, (
            f"{missing} en catalog.py::_WRITE_CATALOG sin entrada en "
            "hermes.capabilities.tool_sensitivity._SAFENT_ADS_WRITE_TOOLS -- "
            "el runtime clasificaria esa escritura como no-SPEND"
        )

    def test_every_live_proposal_tool_is_in_the_runtime_spend_list(
        self, registry: ToolRegistry, runtime_write_tools: frozenset[str]
    ) -> None:
        # Las PROPOSAL fuera de `_WRITE_CATALOG` (propose_campaign,
        # propose_experiment, propose_reallocation_plan, generate_creative_assets)
        # tambien son escrituras del companion: el runtime las audita como SPEND.
        proposal_tool_names = {
            definition.name
            for definition in registry
            if definition.tool_class is ToolClass.PROPOSAL
        }
        assert proposal_tool_names, "el registro vivo no tiene ninguna tool PROPOSAL"
        missing = proposal_tool_names - runtime_write_tools
        assert not missing, (
            f"{missing} son ToolClass.PROPOSAL vivas sin entrada en "
            "hermes.capabilities.tool_sensitivity._SAFENT_ADS_WRITE_TOOLS"
        )


def _parse_frozenset_literal(source: str, var_name: str) -> frozenset[str]:
    """Extrae los literales de cadena de un `frozenset({...})` asignado a
    *var_name*, via AST -- nunca `import hermes`, paquete de otro repo no
    instalado aqui (mismo criterio que la guarda del runtime al reves)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        target = _assigned_name(node)
        if target != var_name or not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.BinOp):
            call = value
        else:
            call = value.left
        if not isinstance(call, ast.Call):
            continue
        (arg,) = call.args
        if not isinstance(arg, ast.Set):
            continue
        return frozenset(
            elt.value
            for elt in arg.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
    raise AssertionError(f"{var_name} no encontrado en {_RUNTIME_TOOL_SENSITIVITY}")


def _assigned_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def test_every_live_proposal_tool_guard_is_collected() -> None:
    """M3 regression (secreview-mac-integration.md): the guard used to sit
    one indent level too deep -- syntactically valid as a nested `def`
    inside `_assigned_name`, after its `return`, but invisible to pytest's
    collector, which only walks module globals and class bodies. Runs a
    real collection pass so the same silent drift fails loudly again."""
    node_id = (
        "TestWriteCatalogMatchesRuntimeSpendClassification::"
        "test_every_live_proposal_tool_is_in_the_runtime_spend_list"
    )
    result = subprocess.run(  # noqa: S603 - trusted interpreter, this file's own resolved path
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert node_id in result.stdout, result.stdout
