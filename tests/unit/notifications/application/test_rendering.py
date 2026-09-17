"""Render de las tarjetas de aprobacion/recibo (contracts/telegram.md):
snapshots en espanol, sin PII ni identificadores externos completos, y
dentro de los limites de longitud de Telegram (4096 caracteres por
mensaje)."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.notifications.application.dto import (
    ApprovalDetailView,
    ApprovalEvidenceLine,
    ApprovalRequestView,
    AutoReceiptEvent,
    CauseGroupApprovalView,
    ConfirmSpendIncreaseView,
    SignalKind,
)
from safent_ads.notifications.application.ports import UndoResultKind
from safent_ads.notifications.application.rendering import (
    render_approval_detail_card,
    render_approval_request_card,
    render_auto_receipt,
    render_batch_outcome,
    render_cause_group_card,
    render_confirm_spend_increase_card,
    render_decision_outcome,
    render_denial_notice,
    render_diff_changed_notice,
    render_expired_notice,
    render_undo_outcome,
)

_TELEGRAM_MESSAGE_LIMIT = 4096


def _approval_view(**overrides: object) -> ApprovalRequestView:
    defaults: dict[str, object] = {
        "proposal_id": "9f3a1c07-1234-4321-8888-abcdefabcdef",
        "diff_hash": "a" * 64,
        "entity_name": "Secundaria Madrid",
        "platform_label": "Google",
        "kind": SignalKind.BUY,
        "parameter_label": "Presupuesto",
        "before_label": "90 €/día",
        "after_label": "117 €/día",
        "change_note": "+30 %",
        "cause_text": "CPL 19 € vs objetivo 28 €, limitada por presupuesto (IS perdida 34 %)",
        "window_label": "7D",
        "impact_label": "+810 €/mes",
        "rule_id": "G01",
        "expires_at": datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        "is_spend_increase": True,
    }
    defaults.update(overrides)
    return ApprovalRequestView(**defaults)  # type: ignore[arg-type]


def test_render_approval_request_card_matches_contract_format() -> None:
    text = render_approval_request_card(_approval_view())

    assert text == (
        "🔺 APROBAR · Secundaria Madrid (Google)\n"
        "Presupuesto 90 €/día → 117 €/día  (+30 %)\n"
        "Motivo: CPL 19 € vs objetivo 28 €, limitada por presupuesto (IS perdida 34 %)\n"
        "Ventana 7D · impacto estimado +810 €/mes · regla G01\n"
        "Caduca 10-sep 14:00"
    )
    assert len(text) <= _TELEGRAM_MESSAGE_LIMIT
    assert "@" not in text  # sin username/handle de terceros


def test_render_approval_request_card_uses_icon_for_kind() -> None:
    sell_text = render_approval_request_card(_approval_view(kind=SignalKind.SELL))
    exit_text = render_approval_request_card(_approval_view(kind=SignalKind.EXIT))

    assert sell_text.startswith("🔻 APROBAR")
    assert exit_text.startswith("⏸ APROBAR")


def test_render_approval_request_card_without_change_note_omits_parenthesis() -> None:
    text = render_approval_request_card(_approval_view(change_note=None))

    assert "Presupuesto 90 €/día → 117 €/día\n" in text
    assert "(" not in text.splitlines()[1]


def test_render_confirm_spend_increase_card_matches_contract_format() -> None:
    view = ConfirmSpendIncreaseView(
        entity_name="Secundaria Madrid",
        before_label="90 €/día",
        after_label="117 €/día",
        impact_label="+810 €/mes",
        monthly_cap_label="1.240 €",
    )

    text = render_confirm_spend_increase_card(view)

    assert text == (
        "⚠️ Confirmar subida\n"
        "Secundaria Madrid · 90 €/día → 117 €/día · +810 €/mes estimado\n"
        "Tope mensual restante tras el cambio: 1.240 €"
    )


def test_render_approval_detail_card_includes_evidence_and_guardrails() -> None:
    detail = ApprovalDetailView(
        evidence=(ApprovalEvidenceLine(metric="cpl", actual=19.0, target=28.0, window_label="7D"),),
        guardrail_floor_label="10 €",
        guardrail_ceiling_label="300 €",
        guardrail_monthly_cap_label="10.000 €",
    )

    text = render_approval_detail_card(_approval_view(), detail)

    assert render_approval_request_card(_approval_view()) in text
    assert "cpl: 19 vs objetivo 28 (7D)" in text
    assert "suelo 10 €" in text
    assert "techo 300 €" in text
    assert "tope mensual 10.000 €" in text


def test_render_cause_group_card_matches_contract_format() -> None:
    items = (
        _approval_view(
            entity_name="Secundaria Madrid", before_label="90 €/día", after_label="117 €/día"
        ),
        _approval_view(
            entity_name="Primaria Valencia", before_label="60 €/día", after_label="78 €/día"
        ),
        _approval_view(
            entity_name="Inglés Andalucía", before_label="45 €/día", after_label="58 €/día"
        ),
    )
    group = CauseGroupApprovalView(
        cause_text="limitadas por presupuesto con CPL bajo objetivo",
        total_impact_label="+1.940 €/mes",
        anchor_proposal_id=items[0].proposal_id,
        anchor_diff_hash=items[0].diff_hash,
        items=items,
    )

    text = render_cause_group_card(group)

    assert text == (
        "🔺 3 campañas · misma causa: limitadas por presupuesto con CPL bajo objetivo\n"
        "Impacto acumulado estimado +1.940 €/mes\n"
        "1. Secundaria Madrid  90 €/día → 117 €/día\n"
        "2. Primaria Valencia  60 €/día → 78 €/día\n"
        "3. Inglés Andalucía  45 €/día → 58 €/día"
    )


def test_render_cause_group_card_caps_visible_lines() -> None:
    items = tuple(_approval_view(entity_name=f"Campaña {i}") for i in range(15))
    group = CauseGroupApprovalView(
        cause_text="misma causa",
        total_impact_label="+1 €/mes",
        anchor_proposal_id=items[0].proposal_id,
        anchor_diff_hash=items[0].diff_hash,
        items=items,
    )

    text = render_cause_group_card(group)

    assert "10." in text
    assert "11." not in text
    assert "⏸ 5 más · /pendientes" in text


def test_render_auto_receipt_matches_contract_format() -> None:
    event = AutoReceiptEvent(
        business_name="Negocio Ejemplo",
        entity_name="Búsqueda Marca",
        platform_label="Meta",
        parameter_label="Presupuesto",
        before_label="120 €/día",
        after_label="84 €/día",
        change_note="−30 %",
        cause_text="ROAS bajo objetivo en 3D y 7D",
        rule_id="M05",
        guardrail_note="suelo 60 €/día",
        change_ordinal=1,
        change_max=2,
        decided_at=datetime(2026, 9, 9, 14, 12, tzinfo=UTC),
        undo_deadline=datetime(2026, 9, 9, 14, 42, tzinfo=UTC),
        proposal_id="9f3a1c07-1234-4321-8888-abcdefabcdef",
        diff_hash="a" * 64,
    )

    text = render_auto_receipt(event)

    assert text == (
        "✅ Automático · Búsqueda Marca (Meta)\n"
        "Presupuesto 120 €/día → 84 €/día  (−30 %)\n"
        "Motivo: ROAS bajo objetivo en 3D y 7D · regla M05\n"
        "Guardarraíl: suelo 60 €/día · 1º cambio de hoy (máx. 2)\n"
        "14:12 · deshacer hasta las 14:42"
    )


def test_render_expired_notice_has_no_pii() -> None:
    text = render_expired_notice(entity_name="Secundaria Madrid", state_label="aprobada")

    assert "Secundaria Madrid" in text
    assert "aprobada" in text
    assert "@" not in text


def test_render_diff_changed_notice_is_explicit() -> None:
    text = render_diff_changed_notice(entity_name="Secundaria Madrid")

    assert "cambió" in text
    assert "Secundaria Madrid" in text


def test_render_decision_outcome_never_leaks_more_than_channel_identity() -> None:
    text = render_decision_outcome(
        entity_name="Secundaria Madrid",
        decision_label="✅ Aprobado",
        decided_by="telegram:111222333",
        decided_at=datetime(2026, 9, 10, 14, 3, tzinfo=UTC),
    )

    assert text == "✅ Aprobado · Secundaria Madrid\npor telegram:111222333 · 10-sep 14:03"


def test_render_denial_notice() -> None:
    text = render_denial_notice(
        entity_name="Secundaria Madrid", reason_label="un guardarraíl lo bloquea"
    )

    assert text == "⚠️ Secundaria Madrid\nNo se pudo aplicar: un guardarraíl lo bloquea"


def test_render_undo_outcome_after_grace_says_not_available() -> None:
    text = render_undo_outcome(entity_name="Búsqueda Marca", kind=UndoResultKind.NOT_ALLOWED)

    assert "Deshacer no disponible" in text
    assert "ya no está disponible" in text


def test_render_undo_outcome_within_grace_says_restored() -> None:
    text = render_undo_outcome(entity_name="Búsqueda Marca", kind=UndoResultKind.RESTORED)

    assert "restauró el valor anterior" in text


def test_render_batch_outcome_lists_partial_failure_explicitly() -> None:
    text = render_batch_outcome(
        cause_text="limitadas por presupuesto con CPL bajo objetivo",
        results=[
            ("Secundaria Madrid", True, None),
            ("Primaria Valencia", False, "la propuesta cambió mientras se decidía"),
            ("Inglés Andalucía", True, None),
        ],
    )

    assert text == (
        "✅ 2/3 aprobadas · misma causa: limitadas por presupuesto con CPL bajo objetivo\n"
        "1. Secundaria Madrid ✅\n"
        "2. Primaria Valencia ❌ (la propuesta cambió mientras se decidía)\n"
        "3. Inglés Andalucía ✅"
    )
