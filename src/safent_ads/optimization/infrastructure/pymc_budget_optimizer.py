"""Adaptador de `ResponseCurveFitterPort` y `BudgetOptimizerPort` sobre
`pymc_marketing.mmm` (profitability-engine.md §3b/§7, assumptions:
"PyMC-Marketing vive en optimization/infrastructure tras un puerto; si no
converge, el dominio sigue con el tactico").

`pymc-marketing>=1.1` esta en `pyproject.toml` (lane "profitability") y
**verificado instalable en aarch64 (DGX) 2026-09-09**: `import
pymc_marketing` y `pymc_marketing.mmm.BudgetOptimizer` cargan sin compilar
nada. `optimization/infrastructure` es el UNICO lugar del contexto que lo
importa -- el dominio (`response_curve.py`) es puro, sin `pymc`/`numpy`.

**Limite deliberado de esta sesion**: el ajuste real es MCMC bayesiano
(minutos, no segundos, y exige >= 52 semanas de historico real que este
entorno no tiene, profitability-engine.md §10). Ningun test de este
repositorio invoca `.fit()` de verdad -- los tests de este adaptador usan
dobles (`unittest.mock`) del modelo pymc para probar la logica de
traduccion y degradacion (r_hat, umbral de semanas, `slope != 1`), nunca
el motor bayesiano en si. Ejecutar un ajuste real contra datos de un
negocio necesita autorizacion explicita aparte (memoria del propietario:
nada de trabajos de computo pesado sin permiso)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from safent_ads.optimization.application.dto import MmmFitResult
from safent_ads.optimization.domain.response_curve import HillCurve

if TYPE_CHECKING:
    import pandas as pd

R_HAT_LOW = 0.99
R_HAT_HIGH = 1.01
MIN_WEEKS_FOR_MMM = 52
_SLOPE_TOLERANCE = 0.2  # slope en [1-tol, 1+tol] se admite como HillCurve simple (§7)
_DEFAULT_ADSTOCK_L_MAX = 8


class ResponseCurveFitUnavailableError(RuntimeError):
    """`pymc-marketing` no disponible, historico insuficiente, MCMC no
    convergio, o la curva ajustada no es representable por el `HillCurve`
    simple de dominio (`slope` lejos de 1). En todos los casos: el dominio
    degrada al estimador tactico (a) -- profitability-engine.md §3."""


class FittedMmmModel(Protocol):
    """Forma minima que este adaptador necesita de un `pymc_marketing.mmm.
    MMM` ya ajustado -- Protocol en vez del tipo concreto para que los
    tests inyecten un doble sin importar `pymc_marketing`."""

    def fit(self, X: Any, y: Any, **kwargs: Any) -> Any: ...  # noqa: ANN401
    def budget_optimizer(self, start_date: Any, end_date: Any, **kwargs: Any) -> Any: ...  # noqa: ANN401


class PymcResponseCurveFitter:
    """Adaptador de `ResponseCurveFitterPort`: Hill + adstock geometrico
    sobre `pymc_marketing.mmm.MMM`."""

    def __init__(
        self, *, adstock_l_max: int = _DEFAULT_ADSTOCK_L_MAX, random_seed: int = 42
    ) -> None:
        self._adstock_l_max = adstock_l_max
        self._random_seed = random_seed

    def fit(
        self,
        *,
        weekly_spend: pd.DataFrame,
        weekly_target: pd.Series,
        channel_columns: list[str],
        control_columns: list[str] | None = None,
    ) -> MmmFitResult:
        _assert_enough_history(len(weekly_spend))
        model = _build_model(
            channel_columns=channel_columns,
            control_columns=control_columns,
            adstock_l_max=self._adstock_l_max,
        )
        idata = _fit_model(model, weekly_spend, weekly_target, random_seed=self._random_seed)
        r_hat_max = _max_r_hat(idata)
        if not (R_HAT_LOW <= r_hat_max <= R_HAT_HIGH):
            raise ResponseCurveFitUnavailableError(
                f"r_hat_max={r_hat_max:.4f} fuera de [{R_HAT_LOW}, {R_HAT_HIGH}]: "
                "el MMM no convergio, manda el estimador tactico (a)"
            )
        curves = _extract_hill_curves(idata, channel_columns)
        return MmmFitResult(curves_by_channel=curves, r_hat_max=r_hat_max)


def _assert_enough_history(weeks: int) -> None:
    if weeks < MIN_WEEKS_FOR_MMM:
        raise ResponseCurveFitUnavailableError(
            f"{weeks} semanas < {MIN_WEEKS_FOR_MMM}: sin MMM, el estimador tactico (a) manda "
            "(profitability-engine.md §3: 'paired-window fallback para <52 semanas')"
        )


def _build_model(
    *, channel_columns: list[str], control_columns: list[str] | None, adstock_l_max: int
) -> Any:  # noqa: ANN401 - tipo de terceros, ver FittedMmmModel para el contrato que usamos
    try:
        from pymc_marketing.mmm import MMM, GeometricAdstock, HillSaturation  # noqa: PLC0415
    except ImportError as exc:
        raise ResponseCurveFitUnavailableError(
            "pymc-marketing no importable en este entorno"
        ) from exc
    return MMM(
        date_column="date",
        channel_columns=channel_columns,
        control_columns=control_columns,
        adstock=GeometricAdstock(l_max=adstock_l_max),
        saturation=HillSaturation(),
    )


def _fit_model(
    model: Any,
    weekly_spend: pd.DataFrame,
    weekly_target: pd.Series,
    *,
    random_seed: int,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    try:
        return model.fit(weekly_spend, weekly_target, random_seed=random_seed)
    except Exception as exc:  # noqa: BLE001 - frontera con un motor bayesiano de terceros
        raise ResponseCurveFitUnavailableError(f"ajuste MMM fallo: {exc}") from exc


def _max_r_hat(idata: Any) -> float:  # noqa: ANN401
    import arviz as az  # noqa: PLC0415

    r_hat_dataset = az.rhat(idata)
    return float(r_hat_dataset.to_array().max().item())


def _extract_hill_curves(idata: Any, channel_columns: list[str]) -> dict[str, HillCurve]:  # noqa: ANN401
    """Traduce el posterior de `HillSaturation` (parametros `slope`,
    `kappa`, `beta`, funcion `beta*x^slope/(x^slope+kappa^slope)`) al
    `HillCurve` simple de dominio (`E_max*S/(S+k)`, slope implicito = 1).
    Solo valido cuando `slope` esta cerca de 1 -- si no, la curva real no
    es representable por la forma simple y se rechaza en vez de mentir."""
    posterior = idata.posterior
    curves: dict[str, HillCurve] = {}
    for i, channel in enumerate(channel_columns):
        slope = float(posterior["saturation_slope"].mean().values[i])
        kappa = float(posterior["saturation_kappa"].mean().values[i])
        beta = float(posterior["saturation_beta"].mean().values[i])
        if abs(slope - 1.0) > _SLOPE_TOLERANCE:
            continue
        curves[channel] = HillCurve(e_max=beta, k=kappa)
    return curves


class PymcBudgetOptimizer:
    """Adaptador de `BudgetOptimizerPort` sobre `BudgetOptimizer.
    allocate_budget()` -- optimiza por canal (§3b: el puente campana-canal
    de `bridge.py` traduce hacia/desde campana antes y despues de esto)."""

    def optimize(
        self,
        *,
        fitted_model: FittedMmmModel,
        start_date: str,
        end_date: str,
        total_budget: float,
        budget_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> dict[str, float]:
        try:
            optimizer = fitted_model.budget_optimizer(start_date, end_date)
            result = optimizer.allocate_budget(total_budget, budget_bounds=budget_bounds)
        except Exception as exc:  # noqa: BLE001 - frontera con scipy.optimize via pymc-marketing
            raise ResponseCurveFitUnavailableError(f"BudgetOptimizer fallo: {exc}") from exc
        return _budgets_to_dict(result.budgets)


def _budgets_to_dict(budgets: Any) -> dict[str, float]:  # noqa: ANN401
    series = budgets.to_series()
    return {str(channel): float(value) for channel, value in series.items()}
