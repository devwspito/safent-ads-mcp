"""`proposal_item` (004 tasks.md A8, contracts/panel.md §2.5): `proposed_by`
es `{kind, label} | null`, nunca un uuid crudo en la fila."""

from __future__ import annotations

from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.presentation.panel_read import proposal_item
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.proposals.conftest import make_proposal


def test_person_proposed_by_surfaces_as_a_person_kind_without_a_raw_uuid() -> None:
    proposal = make_proposal()
    proposal.proposed_by = "person:11111111-1111-1111-1111-111111111111"

    item = proposal_item(proposal, "Campana explicita")

    assert item["proposed_by"] == {"kind": "person", "label": None}
    assert "11111111" not in str(item["proposed_by"])


def test_rule_engine_proposal_has_no_proposed_by() -> None:
    proposal = make_proposal()

    item = proposal_item(proposal, "Campana explicita")

    assert item["proposed_by"] is None


def test_proposal_item_always_reports_item_kind_proposal() -> None:
    """`contracts/api.md` §1 (Revision 2):
    campo aditivo con valor por defecto -- un `Proposal` nunca es un
    paquete. `package_id` (el otro campo aditivo del contrato) todavia no
    se emite aqui: `panel/src/api/schemas/proposals.ts::proposalItemSchema`
    no lo declara y Zod recortaria la clave, rompiendo
    `proposals.contract.test.ts` byte a byte."""
    proposal = make_proposal()

    item = proposal_item(proposal, "Campana explicita")

    assert item["item_kind"] == "proposal"
    assert "package_id" not in item


def _native_write_diff(payload: dict[str, object]) -> ProposedDiff:
    ref = EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "1234567890")
    return ProposedDiff.build(
        entity_ref=ref,
        parameter="native:meta:update_campaign",
        before=None,
        after=payload,
    )


def test_bj5_native_write_requires_typed_confirmation() -> None:
    """Bj-5: una escritura nativa es carga libre sin interpretar -- no
    puede aprobarse con un simple click, igual que crear o borrar."""
    proposal = make_proposal(
        diff=_native_write_diff({"start_time": "2026-01-01T00:00:00+0000"}),
        classification=Classification.CRITICAL,
    )

    item = proposal_item(proposal, "Campana explicita")

    assert item["requires_typed_confirmation"] is True


def test_bj5_native_write_payload_is_shown_as_a_formatted_json_block() -> None:
    """Bj-5: antes se aplastaba a una sola linea (`display_value`, sin
    `indent`) -- el dueño no podia leer de verdad un payload con varias
    claves."""
    proposal = make_proposal(
        diff=_native_write_diff({"name": "x", "start_time": "2026-01-01T00:00:00+0000"}),
        classification=Classification.CRITICAL,
    )

    item = proposal_item(proposal, "Campana explicita")

    assert item["diff"]["valor_propuesto"] == (
        '{\n  "name": "x",\n  "start_time": "2026-01-01T00:00:00+0000"\n}'
    )


def test_bj5_non_native_write_proposal_keeps_the_single_line_display() -> None:
    """Regresion negativa: una propuesta ordinaria (`daily_budget`) no
    cambia de formato ni exige confirmacion tecleada."""
    proposal = make_proposal()

    item = proposal_item(proposal, "Campana explicita")

    assert item["requires_typed_confirmation"] is False
    assert isinstance(item["diff"]["valor_propuesto"], float)
