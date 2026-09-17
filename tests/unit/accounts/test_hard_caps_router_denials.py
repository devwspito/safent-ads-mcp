"""M-8 (revision T035): `DENIED` del broker solo es 404 cuando el motivo es
`invalid_schema`; cualquier otro motivo es un despliegue roto y sale como 503."""

from __future__ import annotations

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.presentation.hard_caps_router import _translate_denial


def test_an_invalid_schema_denial_is_a_404() -> None:
    error = _translate_denial(BrokerRequestDeniedError("DENIED", "invalid_schema"))
    assert (error.status_code, error.detail["code"]) == (404, "NOT_FOUND")


@pytest.mark.parametrize("reason", ["unauthorized_peer", "op_not_available_in_f1", None])
def test_any_other_denial_reason_is_a_503_not_a_404(reason: str | None) -> None:
    error = _translate_denial(BrokerRequestDeniedError("DENIED", reason))
    assert error.status_code == 503
    assert error.detail["code"] != "NOT_FOUND"


def test_the_denial_keeps_its_reason() -> None:
    assert BrokerRequestDeniedError("DENIED", "invalid_schema").reason == "invalid_schema"
