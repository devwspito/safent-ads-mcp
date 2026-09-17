from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from safent_ads.iam.application.action_confirmation import (
    ActionConfirmationCodec,
    ConfirmationError,
)

NOW = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", uuid4()),
        ("method", "PUT"),
        ("path", "/b"),
        ("query", "b=2"),
        ("body", b'{"changed":true}'),
        ("action", "other"),
    ],
)
def test_every_request_component_is_bound(field, value):
    codec = ActionConfirmationCodec("fixture-owner-key-long-enough")
    original = dict(
        session_id=uuid4(), method="POST", path="/a", query="a=1", body=b"{}", action="save"
    )
    proof, _ = codec.issue(binding=codec.binding(**original), now=NOW)
    with pytest.raises(ConfirmationError, match="CONFIRMATION_INVALID"):
        codec.verify(proof, binding=codec.binding(**{**original, field: value}), now=NOW)


@pytest.mark.parametrize("proof", ["", "x", "a.b.c", "%%%.$$$", "é.ñ", "a" * 2100])
def test_malformed_tokens_always_return_safe_error(proof):
    with pytest.raises(ConfirmationError, match="CONFIRMATION_INVALID"):
        ActionConfirmationCodec("fixture-key").verify(proof, binding="b" * 64, now=NOW)


def test_deadline_and_key_rotation_are_fail_closed():
    codec = ActionConfirmationCodec("fixture-key")
    proof, deadline = codec.issue(binding="b" * 64, now=NOW)
    assert deadline == NOW + timedelta(seconds=120)
    assert codec.verify(proof, binding="b" * 64, now=NOW)[0]
    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        codec.verify(proof, binding="b" * 64, now=deadline)
    with pytest.raises(ConfirmationError, match="CONFIRMATION_INVALID"):
        ActionConfirmationCodec("rotated-fixture-key").verify(proof, binding="b" * 64, now=NOW)
