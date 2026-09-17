import { useState } from "react";
import { useConfirmAutonomyGate } from "@/api/queries/rules";
import type { AutonomyGate, AutonomyGateConfirmationInput } from "@/api/schemas/rules";
import { ActionConfirmationDialog } from "@/components/common/ActionConfirmationDialog";
import { useConfirmedMutation } from "@/hooks/useConfirmedMutation";
import styles from "./AutonomyGateQuestions.module.css";

interface AutonomyGateQuestionsProps {
  businessId: string;
  gate: AutonomyGate;
}

function fieldKey(platformAccountId: string, questionKey: string): string {
  return `${platformAccountId}:${questionKey}`;
}

/**
 * Confirma por cuenta las preguntas 2, 3 y 8 de spec.md que bloquean la puerta de autonomía
 * (rest-api.md §Reglas y guardarraíles): cada respuesta exige confirmación humana del contenido exacto,
 * mismo mecanismo (`useConfirmedMutation`) que el resto de mutaciones sensibles del panel.
 */
export function AutonomyGateQuestions({ businessId, gate }: AutonomyGateQuestionsProps) {
  const confirmQuestion = useConfirmAutonomyGate(businessId);
  const [values, setValues] = useState<Record<string, string>>({});

  const confirmation = useConfirmedMutation<AutonomyGateConfirmationInput, unknown>({
    scopeKey: `gate:${businessId}`,
    summarize: (v) => [`Negocio: ${businessId}`, `Cuenta: ${v.platform_account_id}`, `Condición: ${v.key}`, `Respuesta: ${v.value}`],
    mutate: (variables) => confirmQuestion.mutateAsync(variables),
  });

  const pendingAccounts = gate.accounts.filter((account) => !account.ready);
  if (pendingAccounts.length === 0) return null;

  return (
    <div className={styles.section} role="group" aria-label="Confirmaciones pendientes de la puerta de autonomía">
      {pendingAccounts.map((account) => (
        <div key={account.platform_account_id} className={styles.account}>
          <p className={styles.accountLabel}>{account.label}</p>
          {account.missing.map((question) => {
            const key = fieldKey(account.platform_account_id, question.key);
            const value = values[key] ?? question.recommended_default ?? "";
            return (
              <div key={question.key} className={styles.question}>
                <label className={styles.questionLabel} htmlFor={key}>
                  {question.label}
                </label>
                <div className={styles.questionRow}>
                  <input
                    id={key}
                    className={styles.questionInput}
                    value={value}
                    onChange={(event) => setValues((prev) => ({ ...prev, [key]: event.target.value }))}
                  />
                  <button
                    type="button"
                    className={styles.confirmButton}
                    disabled={!value.trim()}
                    onClick={() =>
                      confirmation.start({
                        platform_account_id: account.platform_account_id,
                        key: question.key,
                        value: value.trim(),
                      })
                    }
                  >
                    Confirmar
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ))}

      {confirmation.isPromptOpen ? (
        <ActionConfirmationDialog
          title="Confirmar respuesta de la puerta de autonomía"
          description="Revisa la cuenta y la respuesta antes de habilitar esta condición de autonomía."
          confirmLabel="Confirmar"
          summary={confirmation.summary}
          canConfirm={confirmation.canConfirm}
          isSubmitting={confirmation.isSubmitting}
          errorMessage={confirmation.errorMessage}
          onConfirm={confirmation.confirm}
          onClose={confirmation.cancel}
        />
      ) : null}
    </div>
  );
}
