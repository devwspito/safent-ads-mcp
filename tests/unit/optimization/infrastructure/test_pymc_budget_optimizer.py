"""`PymcResponseCurveFitter`/`PymcBudgetOptimizer` (profitability-engine.md
§3b): logica de traduccion y degradacion contra dobles del modelo pymc --
ningun test invoca MCMC de verdad (ver docstring del modulo bajo prueba)."""

from __future__ import annotations

import builtins
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from safent_ads.optimization.domain.response_curve import HillCurve
from safent_ads.optimization.infrastructure.pymc_budget_optimizer import (
    MIN_WEEKS_FOR_MMM,
    PymcBudgetOptimizer,
    PymcResponseCurveFitter,
    ResponseCurveFitUnavailableError,
)


class _FakeSeries:
    def __init__(self, values: dict[str, float]) -> None:
        self._values = values

    def mean(self) -> _FakeSeries:
        return self

    @property
    def values(self) -> list[float]:
        return list(self._values.values())


def _fake_posterior(
    *, slope: dict[str, float], kappa: dict[str, float], beta: dict[str, float]
) -> object:
    return SimpleNamespace(
        posterior={
            "saturation_slope": _FakeSeries(slope),
            "saturation_kappa": _FakeSeries(kappa),
            "saturation_beta": _FakeSeries(beta),
        }
    )


class TestHistoryThreshold:
    def test_rejects_before_touching_pymc_when_below_52_weeks(self) -> None:
        fitter = PymcResponseCurveFitter()
        weekly_spend = list(range(MIN_WEEKS_FOR_MMM - 1))

        with pytest.raises(ResponseCurveFitUnavailableError, match="semanas"):
            fitter.fit(
                weekly_spend=weekly_spend,
                weekly_target=[],
                channel_columns=["google"],
            )


class TestConvergenceDegradation:
    def test_r_hat_outside_band_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fitter = PymcResponseCurveFitter()
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._build_model",
            lambda **_: object(),
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._fit_model",
            lambda *_, **__: object(),
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._max_r_hat",
            lambda _idata: 1.25,
        )

        with pytest.raises(ResponseCurveFitUnavailableError, match="r_hat"):
            fitter.fit(
                weekly_spend=list(range(MIN_WEEKS_FOR_MMM)),
                weekly_target=list(range(MIN_WEEKS_FOR_MMM)),
                channel_columns=["google"],
            )

    def test_converged_extracts_hill_curve_per_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fitter = PymcResponseCurveFitter()
        idata = _fake_posterior(
            slope={"google": 1.02, "meta": 0.95},
            kappa={"google": 500.0, "meta": 300.0},
            beta={"google": 100.0, "meta": 80.0},
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._build_model",
            lambda **_: object(),
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._fit_model",
            lambda *_, **__: idata,
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._max_r_hat",
            lambda _idata: 1.005,
        )

        result = fitter.fit(
            weekly_spend=list(range(MIN_WEEKS_FOR_MMM)),
            weekly_target=list(range(MIN_WEEKS_FOR_MMM)),
            channel_columns=["google", "meta"],
        )

        assert result.r_hat_max == pytest.approx(1.005)
        assert set(result.curves_by_channel) == {"google", "meta"}
        assert isinstance(result.curves_by_channel["google"], HillCurve)
        assert result.curves_by_channel["google"].e_max == pytest.approx(100.0)
        assert result.curves_by_channel["google"].k == pytest.approx(500.0)

    def test_slope_far_from_one_is_excluded_not_faked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fitter = PymcResponseCurveFitter()
        idata = _fake_posterior(
            slope={"google": 2.5},  # muy lejos de 1: no representable por HillCurve simple
            kappa={"google": 500.0},
            beta={"google": 100.0},
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._build_model",
            lambda **_: object(),
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._fit_model",
            lambda *_, **__: idata,
        )
        monkeypatch.setattr(
            "safent_ads.optimization.infrastructure.pymc_budget_optimizer._max_r_hat",
            lambda _idata: 1.0,
        )

        result = fitter.fit(
            weekly_spend=list(range(MIN_WEEKS_FOR_MMM)),
            weekly_target=list(range(MIN_WEEKS_FOR_MMM)),
            channel_columns=["google"],
        )

        assert result.curves_by_channel == {}

    def test_import_error_degrades_to_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def _blocking_import(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("pymc_marketing"):
                raise ImportError("no pymc_marketing in this sandbox")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocking_import)
        fitter = PymcResponseCurveFitter()

        with pytest.raises(ResponseCurveFitUnavailableError, match="no importable"):
            fitter.fit(
                weekly_spend=list(range(MIN_WEEKS_FOR_MMM)),
                weekly_target=list(range(MIN_WEEKS_FOR_MMM)),
                channel_columns=["google"],
            )


class TestBudgetOptimizerAdapter:
    def test_translates_scipy_result_into_plain_dict(self) -> None:
        fake_series = SimpleNamespace(items=lambda: [("google", 700.0), ("meta", 300.0)])
        fake_result = SimpleNamespace(budgets=SimpleNamespace(to_series=lambda: fake_series))
        fake_optimizer = MagicMock()
        fake_optimizer.allocate_budget.return_value = fake_result
        fitted_model = MagicMock()
        fitted_model.budget_optimizer.return_value = fake_optimizer

        adapter = PymcBudgetOptimizer()
        allocation = adapter.optimize(
            fitted_model=fitted_model,
            start_date="2026-01-01",
            end_date="2026-12-31",
            total_budget=1000.0,
        )

        assert allocation == {"google": 700.0, "meta": 300.0}

    def test_scipy_failure_degrades_to_unavailable(self) -> None:
        fitted_model = MagicMock()
        fitted_model.budget_optimizer.side_effect = RuntimeError("scipy blew up")

        adapter = PymcBudgetOptimizer()
        with pytest.raises(ResponseCurveFitUnavailableError, match="BudgetOptimizer"):
            adapter.optimize(
                fitted_model=fitted_model,
                start_date="2026-01-01",
                end_date="2026-12-31",
                total_budget=1000.0,
            )
