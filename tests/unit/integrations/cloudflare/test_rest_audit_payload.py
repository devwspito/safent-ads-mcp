"""`_connection_decision` (`integrations/cloudflare/rest.py`, revision de
seguridad 2026-09-15, hallazgo medio): conectar/desconectar Cloudflare no
dejaba ningun rastro en `decision_log` -- el payload debe llevar
`account_id`/`zone_count`, NUNCA el token, y `DISCONNECTED` debe conservar
`connected_by_owner_id` (el `DELETE` de `rest.py` borra ese campo de
`cloudflare_connection`; el `decision_log` es la unica historia que
sobrevive)."""

from __future__ import annotations

import uuid

from safent_ads.audit.domain.entry import ActorKind, DecisionKind
from safent_ads.integrations.cloudflare.rest import _connection_decision

_OWNER_EMAIL = "owner@safent.example"
_ACCOUNT_ID = "a" * 32
_OWNER_ID = uuid.uuid4()


def test_connect_decision_never_carries_a_connected_by_owner_id() -> None:
    decision = _connection_decision(
        kind=DecisionKind.CLOUDFLARE_CONNECTED,
        actor_id=_OWNER_EMAIL,
        account_id=_ACCOUNT_ID,
        zone_count=2,
    )

    assert decision.kind is DecisionKind.CLOUDFLARE_CONNECTED
    assert decision.actor_kind is ActorKind.OWNER
    assert decision.actor_id == _OWNER_EMAIL
    assert decision.payload == {"account_id": _ACCOUNT_ID, "zone_count": 2}


def test_disconnect_decision_preserves_who_was_connected() -> None:
    decision = _connection_decision(
        kind=DecisionKind.CLOUDFLARE_DISCONNECTED,
        actor_id=_OWNER_EMAIL,
        account_id=_ACCOUNT_ID,
        zone_count=1,
        connected_by_owner_id=_OWNER_ID,
    )

    assert decision.payload == {
        "account_id": _ACCOUNT_ID,
        "zone_count": 1,
        "connected_by_owner_id": str(_OWNER_ID),
    }


def test_neither_decision_ever_carries_the_token() -> None:
    connect = _connection_decision(
        kind=DecisionKind.CLOUDFLARE_CONNECTED, actor_id=_OWNER_EMAIL, account_id=None, zone_count=0
    )
    disconnect = _connection_decision(
        kind=DecisionKind.CLOUDFLARE_DISCONNECTED,
        actor_id=_OWNER_EMAIL,
        account_id=None,
        zone_count=0,
    )

    assert "token" not in connect.payload
    assert "token" not in disconnect.payload
