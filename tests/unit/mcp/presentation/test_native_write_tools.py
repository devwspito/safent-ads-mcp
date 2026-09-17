"""`native_write_tools.py` (004 tasks-2.md W3): `propose_native_write` es
`PROPOSAL`, verbo-primero, siempre `NATIVE_WRITE` (S-2: nunca la puede
autorizar una regla `AUTO`), y su handler llama al puerto con el payload
ya validado por dominio puro (`mcp/domain/native_write_payload.py`,
aplicado en `presentation/args.py::ProposeNativeWriteArgs`)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.proposal_write_port import ProposalWriteResult
from safent_ads.mcp.presentation.native_write_tools import build_native_write_tool_definitions
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry
from safent_ads.proposals.domain.classification import _ALWAYS_IMPORTANT, ProposalKind

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_ENTITY_REF = "meta:campaign:customers/123/campaigns/456"
_BUSINESS_ID = "0d9f6e4a-3f1a-4a2c-9b7e-1a2b3c4d5e6f"
_WHY = "El equipo de marca pidio ampliar la ventana de conversion a 7 dias."


class _RecordingProposalWritePort:
    def __init__(self) -> None:
        self.calls: dict[str, object] = {}

    async def propose_native_write(self, **kwargs: object) -> ProposalWriteResult:
        self.calls = kwargs
        return ProposalWriteResult(
            proposal_id="proposal-1",
            estado="pendiente",
            diff_hash="a" * 64,
            expires_at=_NOW,
            classification="important",
        )


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="person:00000000-0000-0000-0000-000000000001",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.PROPOSE,
        person_label="Agente de prueba",
    )


def _definition():
    port = _RecordingProposalWritePort()
    definitions = build_native_write_tool_definitions(port)
    return port, {d.name: d for d in definitions}


def test_build_native_write_tool_definitions_registers_a_single_verb() -> None:
    _, by_name = _definition()

    assert set(by_name) == {"propose_native_write"}


def test_es_clase_proposal() -> None:
    _, by_name = _definition()
    registry = ToolRegistry(by_name.values())

    assert registry.get("propose_native_write").tool_class is ToolClass.PROPOSAL  # type: ignore[union-attr]


def test_la_propuesta_nativa_es_siempre_important_y_nunca_la_puede_autorizar_una_regla() -> None:
    assert ProposalKind.NATIVE_WRITE in _ALWAYS_IMPORTANT


async def test_el_handler_reenvia_platform_operation_payload_why_y_proposed_by() -> None:
    port, by_name = _definition()
    args = by_name["propose_native_write"].args_model(
        business_id=_BUSINESS_ID,
        entity_ref=_ENTITY_REF,
        platform="meta",
        operation="update_targeting",
        payload={"headline": "Nuevo anuncio"},
        why=_WHY,
    )

    await by_name["propose_native_write"].handler(args, _caller_scope())

    assert port.calls["platform"] == "meta"
    assert port.calls["operation"] == "update_targeting"
    assert port.calls["payload"] == {"headline": "Nuevo anuncio"}
    assert port.calls["why"] == _WHY
    assert port.calls["proposed_by"] == _caller_scope().caller_id


@pytest.mark.parametrize(
    "payload",
    [
        {"daily_budget": {"amount": "10.00"}},
        {"bid_amount": "1.50"},
        {"access_token": "abc"},
        {"status": "PAUSED"},
    ],
)
def test_args_rechaza_payload_de_presupuesto_puja_token_o_estado(
    payload: dict[str, object],
) -> None:
    _, by_name = _definition()

    with pytest.raises(ValidationError):
        by_name["propose_native_write"].args_model(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            platform="meta",
            operation="update_targeting",
            payload=payload,
            why=_WHY,
        )


def test_args_rechaza_una_url_anidada_mas_alla_de_dos_niveles() -> None:
    """M-3: la barrera anti-URL solo cubria dos niveles de anidamiento --
    una URL a tres niveles de profundidad se colaba antes de este arreglo."""
    _, by_name = _definition()

    with pytest.raises(ValidationError):
        by_name["propose_native_write"].args_model(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            platform="meta",
            operation="update_targeting",
            payload={"config": {"nested": {"target_url": "https://evil.example/x"}}},
            why=_WHY,
        )


def test_args_rechaza_una_url_dentro_de_una_lista() -> None:
    """M-3: la barrera anti-URL no recorria listas en absoluto."""
    _, by_name = _definition()

    with pytest.raises(ValidationError):
        by_name["propose_native_write"].args_model(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            platform="meta",
            operation="update_targeting",
            payload={"tags": ["ok", "https://evil.example/x"]},
            why=_WHY,
        )


def test_args_exige_why_de_al_menos_cuarenta_caracteres() -> None:
    _, by_name = _definition()

    with pytest.raises(ValidationError):
        by_name["propose_native_write"].args_model(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            platform="meta",
            operation="update_targeting",
            payload={"headline": "x"},
            why="demasiado corto",
        )
