"""`optimization_write_tools.py` (004 tasks-2.md W2): las 5 propuestas que
faltaban en el mapa `WriteOperation` <-> herramienta. Registro real, nombres
verbo-primero, clase `PROPOSAL`, y que cada handler llama al metodo correcto
del puerto con los argumentos correctos (incluido `proposed_by`, W6)."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.proposal_write_port import ProposalWriteResult
from safent_ads.mcp.presentation.optimization_write_tools import (
    build_optimization_write_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_ENTITY_REF = "google:campaign:customers/123/campaigns/456"
_BUSINESS_ID = "0d9f6e4a-3f1a-4a2c-9b7e-1a2b3c4d5e6f"

_EXPECTED_NAMES = frozenset(
    {
        "propose_resume",
        "propose_bid_target",
        "propose_negative_keywords",
        "propose_creative_rotation",
        "propose_delete",
    }
)


class _RecordingProposalWritePort:
    """Doble en memoria: registra el ultimo `kwargs` recibido por cada
    metodo, incluido `proposed_by` (W6). Solo implementa lo que este
    modulo llama; el resto no hace falta para este banco."""

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, object]] = {}

    async def propose_resume(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_resume", kwargs)

    async def propose_bid_target(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_bid_target", kwargs)

    async def propose_negative_keywords(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_negative_keywords", kwargs)

    async def propose_creative_rotation(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_creative_rotation", kwargs)

    async def propose_delete(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_delete", kwargs)

    def _record(self, name: str, kwargs: dict[str, object]) -> ProposalWriteResult:
        self.calls[name] = kwargs
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


def _cause() -> dict[str, object]:
    return {"text": "CPL por encima del objetivo en 7D"}


def test_build_optimization_write_tool_definitions_registers_the_five_verbs() -> None:
    definitions = build_optimization_write_tool_definitions(_RecordingProposalWritePort())

    names = {d.name for d in definitions}
    assert names == _EXPECTED_NAMES


def test_all_five_are_proposal_class_and_verb_first() -> None:
    definitions = build_optimization_write_tool_definitions(_RecordingProposalWritePort())

    registry = ToolRegistry(definitions)
    for name in _EXPECTED_NAMES:
        assert registry.get(name) is not None
        assert registry.get(name).tool_class is ToolClass.PROPOSAL  # type: ignore[union-attr]


async def test_propose_resume_forwards_proposed_by_from_a_person_caller_scope() -> None:
    port = _RecordingProposalWritePort()
    definitions = {d.name: d for d in build_optimization_write_tool_definitions(port)}
    args = definitions["propose_resume"].args_model(
        business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF, cause=_cause()
    )

    await definitions["propose_resume"].handler(args, _caller_scope())

    assert port.calls["propose_resume"]["proposed_by"] == _caller_scope().caller_id


async def test_propose_bid_target_forwards_amount_and_currency() -> None:
    port = _RecordingProposalWritePort()
    definitions = {d.name: d for d in build_optimization_write_tool_definitions(port)}
    args = definitions["propose_bid_target"].args_model(
        business_id=_BUSINESS_ID,
        entity_ref=_ENTITY_REF,
        bid_target_amount="1.25",
        bid_target_currency="EUR",
        cause=_cause(),
    )

    await definitions["propose_bid_target"].handler(args, _caller_scope())

    call = port.calls["propose_bid_target"]
    assert call["bid_target_amount"] == "1.25"
    assert call["bid_target_currency"] == "EUR"


async def test_propose_negative_keywords_forwards_the_keyword_tuple() -> None:
    port = _RecordingProposalWritePort()
    definitions = {d.name: d for d in build_optimization_write_tool_definitions(port)}
    args = definitions["propose_negative_keywords"].args_model(
        business_id=_BUSINESS_ID,
        entity_ref=_ENTITY_REF,
        keywords=["gratis", "barato"],
        cause=_cause(),
    )

    await definitions["propose_negative_keywords"].handler(args, _caller_scope())

    assert port.calls["propose_negative_keywords"]["keywords"] == ("gratis", "barato")


async def test_propose_creative_rotation_and_delete_forward_business_and_entity() -> None:
    port = _RecordingProposalWritePort()
    definitions = {d.name: d for d in build_optimization_write_tool_definitions(port)}
    for name in ("propose_creative_rotation", "propose_delete"):
        args = definitions[name].args_model(
            business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF, cause=_cause()
        )

        await definitions[name].handler(args, _caller_scope())

        assert port.calls[name]["business_id"] == _BUSINESS_ID
        assert port.calls[name]["entity_ref"] == _ENTITY_REF
