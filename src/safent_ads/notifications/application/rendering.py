"""Render de texto plano del ticker/digest (contracts/telegram.md §Formatos
de mensaje). Puro: sin I/O, sin HTML — el escape vive en el adaptador de
entrega (T041)."""

from __future__ import annotations

from datetime import datetime

from safent_ads.notifications.application.dto import (
    ApprovalDetailView,
    ApprovalRequestView,
    AutoReceiptEvent,
    CauseGroupApprovalView,
    ConfirmSpendIncreaseView,
    CredentialHealthAlertEvent,
    Money,
    SignalKind,
    TickerSignal,
)
from safent_ads.notifications.application.ports import (
    BrakeStatusView,
    BrakeToggleOutcomeKind,
    BrakeToggleResult,
    BusinessStatusView,
    UndoResultKind,
)
from safent_ads.notifications.domain.brake_confirmation import PendingBrakeAction

_MAX_TICKER_LINES = 12
_MAX_BATCH_LINES = 10
_SEPARATOR = "━" * 16
_SPANISH_MONTH_ABBR = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)

_ICON_BY_KIND = {
    SignalKind.SELL: "🔻",
    SignalKind.BUY: "🔺",
    SignalKind.EXIT: "⏸",
}
_ACTION_BY_KIND = {
    SignalKind.SELL: "BAJAR",
    SignalKind.BUY: "SUBIR",
    SignalKind.EXIT: "SALIR",
}


def sort_by_money_at_stake_desc(signals: list[TickerSignal]) -> list[TickerSignal]:
    """Orden descendente por dinero en juego (contracts/telegram.md
    §Ticker); las senales `HOLD` no son accionables y se descartan antes de
    ordenar, nunca aparecen en el ticker (plan.md `gates.py`)."""
    actionable = [signal for signal in signals if signal.kind is not SignalKind.HOLD]
    return sorted(actionable, key=lambda signal: signal.money_at_stake.amount, reverse=True)


def _format_euros(money: Money) -> str:
    amount = money.amount
    if amount == amount.to_integral_value():
        return f"{amount:.0f} €"
    return f"{amount:.2f} €".replace(".", ",")


def render_ticker_line(signal: TickerSignal) -> str:
    icon = _ICON_BY_KIND[signal.kind]
    action = _ACTION_BY_KIND[signal.kind]
    money = _format_euros(signal.money_at_stake)
    return (
        f"{icon} {signal.entity_name} ({signal.platform_label}) · "
        f"{action} {signal.strength} · {signal.cause_text} · "
        f"{signal.window_label} · {money} en juego"
    )


def render_ticker_body(
    *, business_name: str, header_time_label: str, signals: list[TickerSignal]
) -> str:
    """Cuerpo completo: cabecera, separador, hasta 12 lineas por dinero en
    juego descendente, y un pie con el resto si sobran."""
    ordered = sort_by_money_at_stake_desc(signals)
    visible = ordered[:_MAX_TICKER_LINES]
    remainder = len(ordered) - len(visible)

    lines = [f"📊 {business_name} · {header_time_label}", _SEPARATOR]
    lines.extend(render_ticker_line(signal) for signal in visible)
    if remainder > 0:
        lines.append(f"⏸ {remainder} señales menores · /pendientes")
    return "\n".join(lines)


def render_critical_body(
    *, business_name: str, title: str, detail: str, note: str, time_label: str
) -> str:
    return f"🚨 CRÍTICO · {business_name}\n{title}\n{detail}\n{time_label} · {note}"


def render_credential_health_alert(event: CredentialHealthAlertEvent) -> str:
    """Alerta de `PublishCredentialHealthAlert` (threat-model.md C-21,
    tasks.md T126): sin PII ni token, solo el nombre de la cuenta y el
    codigo corto del motivo."""
    reason = f" ({event.error_code})" if event.error_code else ""
    time_label = _format_datetime_label(event.occurred_at)
    return (
        f"🔑 CREDENCIAL · {event.business_name}\n"
        f"{event.account_label}: {event.health_label}{reason}\n"
        f"{time_label}"
    )


def _format_day_month(moment: datetime) -> str:
    """`10-sep`, en espanol (contracts/telegram.md: "Caduca 10-sep 14:00").
    Abreviaturas fijas: `strftime("%b")` depende del locale del proceso, y
    este sistema nunca debe depender de que alguien active `es_ES` en el
    contenedor."""
    return f"{moment.day}-{_SPANISH_MONTH_ABBR[moment.month - 1]}"


def _format_datetime_label(moment: datetime) -> str:
    return f"{_format_day_month(moment)} {moment:%H:%M}"


def render_approval_request_card(view: ApprovalRequestView) -> str:
    """Nivel 1 (contracts/telegram.md §Solicitud de aprobacion)."""
    icon = _ICON_BY_KIND[view.kind]
    change_suffix = f"  ({view.change_note})" if view.change_note else ""
    return (
        f"{icon} APROBAR · {view.entity_name} ({view.platform_label})\n"
        f"{view.parameter_label} {view.before_label} → {view.after_label}{change_suffix}\n"
        f"Motivo: {view.cause_text}\n"
        f"Ventana {view.window_label} · impacto estimado {view.impact_label} · "
        f"regla {view.rule_id}\n"
        f"Caduca {_format_datetime_label(view.expires_at)}"
    )


def render_confirm_spend_increase_card(view: ConfirmSpendIncreaseView) -> str:
    """Segundo toque obligatorio para SUBIR (contracts/telegram.md)."""
    return (
        f"⚠️ Confirmar subida\n"
        f"{view.entity_name} · {view.before_label} → {view.after_label} · "
        f"{view.impact_label} estimado\n"
        f"Tope mensual restante tras el cambio: {view.monthly_cap_label}"
    )


def render_approval_detail_card(base: ApprovalRequestView, detail: ApprovalDetailView) -> str:
    """Nivel 2 (contracts/telegram.md `[Detalle]`): la tarjeta nivel 1 mas
    evidencia y guardarraíles. No consume el nonce de aprobacion, asi que el
    llamador nunca genera botones nuevos para esta vista (mismo mensaje,
    mismo teclado -- `notifications/application/resolve_callback.py`)."""
    evidence_lines = "\n".join(
        f"· {item.metric}: {item.actual:g} vs objetivo {item.target:g} ({item.window_label})"
        for item in detail.evidence
    )
    return (
        f"{render_approval_request_card(base)}\n"
        f"{_SEPARATOR}\n"
        f"Evidencia:\n{evidence_lines}\n"
        f"Guardarraíles: suelo {detail.guardrail_floor_label} · "
        f"techo {detail.guardrail_ceiling_label} · "
        f"tope mensual {detail.guardrail_monthly_cap_label}"
    )


def render_cause_group_card(group: CauseGroupApprovalView) -> str:
    """Lote por causa (contracts/telegram.md §Lote por causa)."""
    visible = group.items[:_MAX_BATCH_LINES]
    remainder = len(group.items) - len(visible)
    lines = [
        f"🔺 {len(group.items)} campañas · misma causa: {group.cause_text}",
        f"Impacto acumulado estimado {group.total_impact_label}",
    ]
    lines.extend(
        f"{index}. {item.entity_name}  {item.before_label} → {item.after_label}"
        for index, item in enumerate(visible, start=1)
    )
    if remainder > 0:
        lines.append(f"⏸ {remainder} más · /pendientes")
    return "\n".join(lines)


def render_auto_receipt(view: AutoReceiptEvent) -> str:
    """Recibo de accion autonoma (contracts/telegram.md
    §Recibo de accion autonoma)."""
    change_suffix = f"  ({view.change_note})" if view.change_note else ""
    return (
        f"✅ Automático · {view.entity_name} ({view.platform_label})\n"
        f"{view.parameter_label} {view.before_label} → {view.after_label}{change_suffix}\n"
        f"Motivo: {view.cause_text} · regla {view.rule_id}\n"
        f"Guardarraíl: {view.guardrail_note} · "
        f"{view.change_ordinal}º cambio de hoy (máx. {view.change_max})\n"
        f"{view.decided_at:%H:%M} · deshacer hasta las {view.undo_deadline:%H:%M}"
    )


def render_expired_notice(*, entity_name: str, state_label: str) -> str:
    """Regla 1 (contracts/telegram.md): nonce desconocido/caducado/
    consumido -- se reedita el mensaje con el estado real."""
    return f"⏳ {entity_name}\nEsta acción ya no está disponible · estado: {state_label}"


def render_diff_changed_notice(*, entity_name: str) -> str:
    """INV-1 (contracts/telegram.md regla 2): la propuesta cambio desde que
    se pinto la tarjeta -- se reemite con nonce nuevo, nunca se aprueba a
    ciegas sobre terminos distintos a los que el propietario vio."""
    return (
        f"🔄 {entity_name}\n"
        "La propuesta cambió desde que se envió esta tarjeta. Revisa los términos:"
    )


def render_decision_outcome(
    *, entity_name: str, decision_label: str, decided_by: str, decided_at: datetime
) -> str:
    """Desenlace congelado tras resolver una decision (contracts/telegram.md
    regla 4: "el mensaje queda congelado"). `decided_by` nunca lleva datos
    de terceros: es la identidad de canal (p. ej. `telegram:<chat_id>`)."""
    return (
        f"{decision_label} · {entity_name}\npor {decided_by} · {_format_datetime_label(decided_at)}"
    )


def render_denial_notice(*, entity_name: str, reason_label: str) -> str:
    return f"⚠️ {entity_name}\nNo se pudo aplicar: {reason_label}"


_UNDO_OUTCOME_LABELS: dict[UndoResultKind, str] = {
    UndoResultKind.CANCELLED: "↩️ Cancelado",
    UndoResultKind.RESTORED: "↩️ Deshecho",
    UndoResultKind.COMPENSATING_PROPOSAL_CREATED: "↩️ Fuera de la ventana de gracia",
    UndoResultKind.NOT_ALLOWED: "⚠️ Deshacer no disponible",
    UndoResultKind.ALREADY_UNDONE: "↩️ Ya se deshizo",
}
_UNDO_OUTCOME_DETAILS: dict[UndoResultKind, str] = {
    UndoResultKind.CANCELLED: "La ejecución programada se canceló antes de aplicarse.",
    UndoResultKind.RESTORED: "Se restauró el valor anterior.",
    UndoResultKind.COMPENSATING_PROPOSAL_CREATED: (
        "La ventana de gracia ya pasó: se creó una propuesta de restauración "
        "pendiente de aprobación."
    ),
    UndoResultKind.NOT_ALLOWED: "Deshacer ya no está disponible.",
    UndoResultKind.ALREADY_UNDONE: "Esta ejecución ya se deshizo antes.",
}


def render_undo_outcome(*, entity_name: str, kind: UndoResultKind) -> str:
    """Recibo de `[↩️ Deshacer]` (contracts/telegram.md §Recibo de accion
    autonoma): dentro de gracia cancela/restaura, fuera de gracia crea una
    propuesta compensatoria, y si ya no hay nada que deshacer lo dice."""
    return f"{_UNDO_OUTCOME_LABELS[kind]} · {entity_name}\n{_UNDO_OUTCOME_DETAILS[kind]}"


def render_business_status_card(*, business_name: str, status: BusinessStatusView) -> str:
    """`/estado` (este branch): un resumen por negocio. `daily_cap=None`
    (sin guardarrail sembrado) se dice tal cual -- nunca se inventa un
    tope."""
    cap_label = _format_euros(status.daily_cap) if status.daily_cap is not None else "sin tope"
    brake_line = _render_brake_summary_line(status.brake_engaged, status.brake_mode)
    freshness_line = _render_freshness_line(
        lag_minutes=status.freshness_lag_minutes, is_stale=status.freshness_is_stale
    )
    return (
        f"📊 {business_name}\n"
        f"Gasto hoy {_format_euros(status.spend_today)} · tope diario {cap_label}\n"
        f"Ritmo {status.pacing_index_pct:.0f}% · proyección mensual "
        f"{status.pacing_projection_pct:.0f}%\n"
        f"📶 {status.open_signals} señales abiertas · "
        f"📋 {status.pending_proposals} propuestas pendientes\n"
        f"{brake_line}\n"
        f"{freshness_line}"
    )


def _render_brake_summary_line(engaged: bool, mode: str | None) -> str:
    if not engaged:
        return "🟢 Sin freno"
    return f"🔴 Freno activo ({mode})" if mode else "🔴 Freno activo"


def _render_freshness_line(*, lag_minutes: int, is_stale: bool) -> str:
    if is_stale:
        return f"⚠️ Datos con {lag_minutes} min de retraso"
    return f"✅ Datos al día ({lag_minutes} min)"


def render_no_pending_proposals() -> str:
    return "No hay propuestas pendientes."


_BRAKE_PROMPT_BY_ACTION: dict[PendingBrakeAction, str] = {
    PendingBrakeAction.ENGAGE: (
        "⚠️ Confirmar freno\n"
        "¿Activar el freno de emergencia? Bloquea toda escritura hasta que lo apagues."
    ),
    PendingBrakeAction.RELEASE: (
        "⚠️ Confirmar freno\n¿Apagar el freno de emergencia? Se reanuda la actuación automática."
    ),
}


def render_brake_confirm_prompt(action: PendingBrakeAction) -> str:
    """Segundo toque obligatorio de `/freno on|off` (contracts/telegram.md:
    "requiere segundo toque de confirmacion")."""
    return _BRAKE_PROMPT_BY_ACTION[action]


def render_brake_invalid_usage() -> str:
    return "Uso: /freno, /freno on o /freno off."


def render_brake_status(status: BrakeStatusView) -> str:
    if not status.engaged:
        return "🟢 Freno: apagado"
    since_label = f" · desde {_format_datetime_label(status.since)}" if status.since else ""
    reason_line = f"\nMotivo: {status.reason}" if status.reason else ""
    return f"🔴 Freno: activado ({status.mode}){since_label}{reason_line}"


_BRAKE_OUTCOME_LABELS: dict[BrakeToggleOutcomeKind, str] = {
    BrakeToggleOutcomeKind.ENGAGED: "🔴 Freno activado.",
    BrakeToggleOutcomeKind.RELEASED: "🟢 Freno apagado.",
    BrakeToggleOutcomeKind.ALREADY_ENGAGED: "El freno ya estaba activado.",
    BrakeToggleOutcomeKind.NOT_REGISTERED: "No había ningún freno activo.",
}


def render_brake_toggle_outcome(result: BrakeToggleResult) -> str:
    return _BRAKE_OUTCOME_LABELS[result.kind]


def render_brake_cancelled() -> str:
    return "Cancelado. El freno sigue igual."


def render_brake_confirmation_expired() -> str:
    return "Caducado. Repite /freno on o /freno off."


def render_batch_outcome(*, cause_text: str, results: list[tuple[str, bool, str | None]]) -> str:
    """Ejecucion parcial explicita (contracts/telegram.md §Lote por causa:
    "si alguna falla, el mensaje final lista exactamente cual y por que").
    `results` es `(entity_name, ok, reason_if_failed)`."""
    approved = sum(1 for _, ok, _ in results if ok)
    lines = [f"✅ {approved}/{len(results)} aprobadas · misma causa: {cause_text}"]
    for index, (entity_name, ok, reason) in enumerate(results, start=1):
        mark = "✅" if ok else f"❌ ({reason})"
        lines.append(f"{index}. {entity_name} {mark}")
    return "\n".join(lines)
