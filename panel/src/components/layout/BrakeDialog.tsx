import { useRef, useState } from "react";
import { describeApiError } from "@/utils/apiError";
import type { BrakeMode, BrakeScopeKind, KillSwitchItem, KillSwitchState } from "@/api/schemas";
import { Modal } from "@/components/common/Modal";
import { formatExactDate } from "@/utils/time";
import styles from "./BrakeDialog.module.css";

export interface EngageBrakeInput {
  scope_kind: BrakeScopeKind;
  mode: BrakeMode;
  reason: string;
}

interface BrakeDialogProps {
  killSwitch: KillSwitchState | undefined;
  onEngage: (input: EngageBrakeInput) => void | Promise<unknown>;
  /** El llamador envía el `typed_confirmation` fijo que el servidor exige (design.md §14: la hoja ya no lo pide teclear). */
  onRelease: (item: KillSwitchItem, reason: string) => void | Promise<unknown>;
  onClose: () => void;
}

type Step = { kind: "list" } | { kind: "engage" } | { kind: "release"; item: KillSwitchItem };

const SCOPE_LABELS: Record<BrakeScopeKind, string> = {
  global: "Todo el panel",
  business: "Este negocio",
  platform_account: "Una cuenta de plataforma",
};

/**
 * «Parar cambios» — acumulable por ámbito (rest-api.md §Ejecución, deshacer y freno): pararlo
 * cuesta una pulsación. Reanudarlo también es de un solo botón (design.md §14, enmienda del
 * integrador): no es destructivo ni irreversible, se puede volver a parar con un clic, así que
 * teclear una palabra sería fricción sin ganancia de seguridad real. El nombre técnico
 * "kill switch" no se muestra nunca.
 */
export function BrakeDialog({ killSwitch, onEngage, onRelease, onClose }: BrakeDialogProps) {
  const items = killSwitch?.items ?? [];
  const [step, setStep] = useState<Step>(items.length > 0 ? { kind: "list" } : { kind: "engage" });

  if (step.kind === "release") {
    return <ResumeConfirmForm item={step.item} onRelease={onRelease} onClose={onClose} />;
  }

  if (step.kind === "engage") {
    return <EngageBrakeForm onEngage={onEngage} onClose={onClose} />;
  }

  return (
    <Modal label="Cambios parados" onClose={onClose}>
      <div className={styles.body}>
        <p className={styles.title}>Cambios parados</p>
        <div className={styles.list}>
          {items.map((item) => (
            <div key={item.brake_id} className={styles.item}>
              <div>
                <span className={styles.itemLabel}>{item.scope_label}</span>
                <span className={styles.itemMeta}>
                  {item.mode === "ALL" ? "Todo parado" : "Solo lo automático"} · desde {formatExactDate(item.engaged_at)}
                  {item.reason ? ` · ${item.reason}` : ""}
                </span>
              </div>
              <button type="button" className={styles.release} onClick={() => setStep({ kind: "release", item })}>
                Reanudar
              </button>
            </div>
          ))}
        </div>
        <button type="button" className={styles.addBrake} onClick={() => setStep({ kind: "engage" })}>
          Parar en otro ámbito
        </button>
        <div className={styles.actions}>
          <button type="button" className={styles.close} onClick={onClose}>
            Cerrar
          </button>
        </div>
      </div>
    </Modal>
  );
}

function EngageBrakeForm({ onEngage, onClose }: { onEngage: (input: EngageBrakeInput) => void | Promise<unknown>; onClose: () => void }) {
  const [scopeKind, setScopeKind] = useState<Extract<BrakeScopeKind, "business" | "global">>("business");
  const [mode, setMode] = useState<BrakeMode>("ALL");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitted = useRef(false);

  return (
    <Modal label="Parar los cambios" onClose={onClose} busy={busy}>
      <div className={styles.body}>
        <p className={styles.title}>Parar los cambios</p>
        <p className={styles.empty}>
          {mode === "ALL"
            ? "Ningún cambio se aplicará hasta que reanudes; lo que se proponga quedará esperando tu aprobación."
            : "Solo se paran los cambios automáticos; lo que tú apruebes sigue adelante."}
        </p>

        <fieldset className={styles.list} disabled={busy}>
          <legend className="visually-hidden">Ámbito</legend>
          {(["business", "global"] as const).map((option) => (
            <label key={option} className={styles.item}>
              <span className={styles.itemLabel}>{SCOPE_LABELS[option]}</span>
              <input type="radio" name="brake-scope" checked={scopeKind === option} onChange={() => setScopeKind(option)} />
            </label>
          ))}
        </fieldset>

        <fieldset className={styles.list} disabled={busy}>
          <legend className="visually-hidden">Qué para</legend>
          <label className={styles.item}>
            <span className={styles.itemLabel}>Todo</span>
            <input type="radio" name="brake-mode" checked={mode === "ALL"} onChange={() => setMode("ALL")} />
          </label>
          <label className={styles.item}>
            <span className={styles.itemLabel}>Solo lo automático</span>
            <input type="radio" name="brake-mode" checked={mode === "AUTONOMOUS"} onChange={() => setMode("AUTONOMOUS")} />
          </label>
        </fieldset>

        <label className={styles.itemLabel} htmlFor="brake-reason">
          Motivo (opcional)
        </label>
        <input
          disabled={busy}
          id="brake-reason"
          className={styles.textInput}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Por qué paras los cambios"
        />

        {error ? <p role="alert">{error}</p> : null}
        <div className={styles.actions}>
          <button type="button" className={styles.close} onClick={onClose} disabled={busy}>
            Cancelar
          </button>
          <button
            type="button"
            className={styles.addBrake}
            disabled={busy}
            onClick={async () => {
              if (submitted.current) return;
              submitted.current = true;
              setBusy(true);
              setError(null);
              try { await onEngage({ scope_kind: scopeKind, mode, reason: reason || "Parado desde el panel" }); }
              catch (failure) { setError(describeApiError(failure)); }
              finally { submitted.current = false; setBusy(false); }
            }}
          >
            {busy ? "Parando…" : "Parar cambios"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Un solo botón — design.md §14: "¿Reanudar los cambios?" sin nada que teclear. */
function ResumeConfirmForm({
  item,
  onRelease,
  onClose,
}: {
  item: KillSwitchItem;
  onRelease: (item: KillSwitchItem, reason: string) => void | Promise<unknown>;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitted = useRef(false);

  async function handleConfirm() {
    if (submitted.current) return;
    submitted.current = true;
    setBusy(true);
    setError(null);
    try {
      await onRelease(item, "Reanudado desde el panel");
    } catch (failure) {
      setError(describeApiError(failure));
      submitted.current = false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal label="¿Reanudar los cambios?" onClose={onClose} busy={busy}>
      <div className={styles.body}>
        <p className={styles.title}>¿Reanudar los cambios?</p>
        <p className={styles.empty}>
          Safent volverá a aplicar lo que apruebes y sus propios ajustes automáticos en {item.scope_label.toLowerCase()}.
        </p>
        {error ? <p role="alert">{error}</p> : null}
        <div className={styles.actions}>
          <button type="button" className={styles.close} onClick={onClose} disabled={busy}>
            Cancelar
          </button>
          <button type="button" className={styles.addBrake} disabled={busy} onClick={() => void handleConfirm()}>
            {busy ? "Reanudando…" : "Reanudar"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
