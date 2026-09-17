"""`ConversionGoal` (data-model.md
§`ConversionGoal`, tasks.md T012). Shape only -- account membership and
`ENABLED` status are the broker's job (T032), never the domain's."""

from __future__ import annotations

import pytest

from safent_ads.proposals.domain.conversion_goal import ConversionGoal, ConversionGoalError


def test_forma_valida() -> None:
    goal = ConversionGoal(resource_name="customers/1234567890/conversionActions/987654321")

    assert goal.resource_name == "customers/1234567890/conversionActions/987654321"
    assert goal.customer_id == "1234567890"


@pytest.mark.parametrize(
    "resource_name",
    [
        "",
        "customers/abc/conversionActions/1",
        "customers/1/conversionAction/1",
        "customers//conversionActions/1",
        "customers/1/conversionActions/",
        "customers/1/conversionActions/1/",
        " customers/1/conversionActions/1",
        "customers/1/conversionActions/1 ",
        "customers/123456789012345678901/conversionActions/1",
    ],
)
def test_nombre_de_recurso_malformado_falla(resource_name: str) -> None:
    with pytest.raises(ConversionGoalError):
        ConversionGoal(resource_name=resource_name)
