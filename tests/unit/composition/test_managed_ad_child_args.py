"""Regresion: `ManagedAdChildArgs` debe admitir las URLs declaradas del
`child_plan` (contrato de anuncio Google/Meta) sin abrir la puerta a URLs
libres en el resto de campos -- mismo criterio que `ProposeAdChildArgs`
(args.py)."""

import pytest
from pydantic import ValidationError

from safent_ads.composition.managed_service import ManagedAdChildArgs
from tests.unit.broker.platforms.test_ad_child_creation import child_plan


def _args(**overrides: object) -> dict[str, object]:
    base = {
        "entity_ref": "google:ad_set:11111111-1111-1111-1111-111111111111:222:333",
        "child_plan": child_plan("google", "ad"),
        "cause": {"text": "Propuesta administrada explicita"},
    }
    base.update(overrides)
    return base


def test_managed_ad_child_args_admits_the_ad_final_url() -> None:
    args = ManagedAdChildArgs.model_validate(_args())
    assert args.child_plan.model_dump()["native"]["final_url"] == "https://example.com/landing"


def test_managed_ad_child_args_still_rejects_a_url_smuggled_in_cause_text() -> None:
    with pytest.raises(ValidationError, match="no se admiten URLs"):
        ManagedAdChildArgs.model_validate(
            _args(cause={"text": "visita https://evil.example/track"})
        )
