import { useState } from "react";
import { useSendTelegramTestMessage, useStartTelegramPairing, useTelegramPairing, useUnpairTelegram } from "@/api/queries/connections";
import { ActionConfirmationDialog } from "@/components/common/ActionConfirmationDialog";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { useConfirmedMutation } from "@/hooks/useConfirmedMutation";
import { formatRelativeTime } from "@/utils/time";
import styles from "./TelegramPairingCard.module.css";

interface TelegramPairingCardProps {
  businessId: string;
}

/** Telegram: emparejar con código, verificar identidad, envío de prueba, desemparejar — panel-interaction-spec.md §3.8. */
export function TelegramPairingCard({ businessId }: TelegramPairingCardProps) {
  const pairing = useTelegramPairing(businessId);
  const startPairing = useStartTelegramPairing(businessId);
  const sendTest = useSendTelegramTestMessage();
  const unpair = useUnpairTelegram(businessId);
  const [confirmingUnpair, setConfirmingUnpair] = useState(false);

  const pairingConfirmation = useConfirmedMutation<Record<string, never>, { pairing_code: string; code_expires_at: string }>({
    scopeKey: `telegram:${businessId}`,
    summarize: () => ["Generar un código para Telegram del propietario."],
    mutate: (variables) => startPairing.mutateAsync(variables),
  });

  const status = pairing.data?.status;

  return (
    <div className={styles.card}>
      <p className={styles.title}>Telegram</p>
      {status === "paired" ? (
        <>
          <span className={styles.status}>
            Emparejado {pairing.data?.paired_at ? formatRelativeTime(pairing.data.paired_at) : ""}. Chat verificado
            {pairing.data?.chat_id_masked ? ` (${pairing.data.chat_id_masked})` : ""}.
          </span>
          <div className={styles.actions}>
            <button type="button" className={styles.button} onClick={() => sendTest.mutate()} disabled={sendTest.isPending}>
              {sendTest.isPending ? "Enviando…" : "Enviar mensaje de prueba"}
            </button>
            {sendTest.isSuccess ? <span className={styles.status}>Enviado.</span> : null}
            <button type="button" className={styles.button} onClick={() => setConfirmingUnpair(true)}>
              Desemparejar
            </button>
          </div>
        </>
      ) : status === "pending" ? (
        <>
          <span className={styles.status}>Escribe este código al bot desde Telegram:</span>
          <span className={styles.code}>{pairing.data?.pairing_code}</span>
        </>
      ) : (
        <>
          <span className={styles.status}>Sin emparejar todavía.</span>
          <div className={styles.actions}>
            <button type="button" className={styles.button} onClick={() => pairingConfirmation.start({})} disabled={pairingConfirmation.isSubmitting}>
              Emparejar
            </button>
          </div>
        </>
      )}

      {pairingConfirmation.isPromptOpen ? (
        <ActionConfirmationDialog
          title="Confirmar emparejamiento de Telegram"
          description="Se generará un código para vincular tu Telegram. Tendrás que introducirlo en el bot."
          confirmLabel="Emparejar"
          summary={pairingConfirmation.summary}
          canConfirm={pairingConfirmation.canConfirm}
          isSubmitting={pairingConfirmation.isSubmitting}
          errorMessage={pairingConfirmation.errorMessage}
          onConfirm={pairingConfirmation.confirm}
          onClose={pairingConfirmation.cancel}
        />
      ) : null}

      {confirmingUnpair ? (
        <TypedConfirmDialog
          title="Desemparejar Telegram"
          description="Dejarás de recibir notificaciones por este canal hasta volver a emparejar. Escribe DESEMPAREJAR para confirmar."
          confirmLabel="Desemparejar"
          confirmWord="DESEMPAREJAR"
          danger
          onConfirm={() => {
            unpair.mutate("DESEMPAREJAR");
            setConfirmingUnpair(false);
          }}
          onClose={() => setConfirmingUnpair(false)}
        />
      ) : null}
    </div>
  );
}
