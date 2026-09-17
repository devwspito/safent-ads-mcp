import { useState } from "react";
import { ApiRequestError } from "@/api/client";
import { useMe } from "@/api/queries/auth";
import { useKillSwitch, useSetKillSwitch } from "@/api/queries/killSwitch";
import { useAutonomyGate, useGuardrails, useRules, useUpdateGuardrail, useUpdateRule } from "@/api/queries/rules";
import type { GuardrailUpdate, Rule } from "@/api/schemas/rules";
import { BrakeDialog, type EngageBrakeInput } from "@/components/layout/BrakeDialog";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { PageHeader } from "@/components/layout/PageHeader";
import { AutonomyGateQuestions } from "@/components/rules/AutonomyGateQuestions";
import { GuardrailCard } from "@/components/rules/GuardrailCard";
import { RuleRow } from "@/components/rules/RuleRow";
import { autonomyGateSummary, autonomyLevelForSwitch, type RuleSwitchState } from "@/utils/rules";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { describeApiError } from "@/utils/apiError";
import { ErrorState } from "@/components/states/ErrorState";
import styles from "./ReglasPage.module.css";

export function ReglasPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);

  const rulesQuery = useRules(businessId);
  const guardrailsQuery = useGuardrails(businessId);
  const gateQuery = useAutonomyGate(businessId);
  const updateRule = useUpdateRule(businessId);
  const updateGuardrail = useUpdateGuardrail(businessId);
  const killSwitch = useKillSwitch(businessId);
  const setKillSwitch = useSetKillSwitch(businessId);

  const [pendingGuardrail, setPendingGuardrail] = useState<{ guardrailId: string; update: GuardrailUpdate } | null>(null);
  const [brakeDialogOpen, setBrakeDialogOpen] = useState(false);
  const [gateBlockedNotice, setGateBlockedNotice] = useState<string | null>(null);

  const gate = autonomyGateSummary(gateQuery.data);

  async function handleSwitchChange(rule: Rule, state: RuleSwitchState) {
    try {
      await updateRule.mutateAsync({
        rule,
        patch: { is_enabled: state !== "apagada", autonomy_level: autonomyLevelForSwitch(state) },
      });
      setGateBlockedNotice(null);
    } catch (error) {
      if (error instanceof ApiRequestError && error.code === "AUTONOMY_GATE_OPEN") {
        setGateBlockedNotice(`"${rule.name}" no puede pasar a Automática: ${gate.blockedReason ?? "faltan confirmaciones por cuenta"}.`);
        return;
      }
      setGateBlockedNotice(describeApiError(error));
    }
  }

  const engaged = killSwitch.data?.effective.engaged ?? false;

  async function handleEngageBrake(input: EngageBrakeInput) {
    await setKillSwitch.mutateAsync({ scope_kind: input.scope_kind, scope_id: input.scope_kind === "business" ? businessId : null, mode: input.mode, engaged: true, reason: input.reason });
    setBrakeDialogOpen(false);
  }

  return (
    <div>
      <PageHeader title="Reglas" />
      {gateQuery.isError ? <ErrorState message="No se ha podido comprobar la autorización de autonomía. No actives cambios automáticos hasta actualizar." onRetry={() => void gateQuery.refetch()} /> : null}
      {updateRule.isError || updateGuardrail.isError ? <p role="alert" className={styles.gateBanner}>{describeApiError(updateRule.error ?? updateGuardrail.error)}</p> : null}

      {gateQuery.data && !gate.ready ? (
        <div className={styles.gateBanner} role="status">
          Autonomía deshabilitada: {gate.blockedReason}
        </div>
      ) : null}
      {gateBlockedNotice ? (
        <div className={styles.gateBanner} role="alert">
          {gateBlockedNotice}
        </div>
      ) : null}

      {gateQuery.data ? <AutonomyGateQuestions businessId={businessId} gate={gateQuery.data} /> : null}

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Freno de emergencia</h2>
        <div className={styles.brakeRow}>
          <span>{!killSwitch.data || killSwitch.isError ? "No se ha podido confirmar el estado del freno." : engaged ? "El freno está activo: nada se ejecuta de forma autónoma." : "El freno está desactivado."}</span>
          <button
            type="button"
            className={`${styles.brakeButton} ${engaged ? styles.brakeButtonEngaged : ""}`}
            onClick={() => setBrakeDialogOpen(true)}
            disabled={!killSwitch.data || killSwitch.isError || setKillSwitch.isPending}
          >
            {engaged ? "Reanudar" : "Parar cambios"}
          </button>
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Catálogo de reglas</h2>
        <QueryBoundary
          isLoading={rulesQuery.isLoading}
          isError={rulesQuery.isError}
          error={rulesQuery.error}
          onRetry={() => void rulesQuery.refetch()}
          data={rulesQuery.data}
          isEmpty={(data) => data.items.length === 0}
          emptyTitle="Sin reglas configuradas"
          emptyBody="Todavía no hay reglas en el catálogo de este negocio."
        >
          {(data) => (
            <fieldset className={styles.catalog} disabled={updateRule.isPending || rulesQuery.isFetching || gateQuery.isError}>
              {data.items.map((rule) => (
                <RuleRow
                  key={rule.rule_id}
                  rule={rule}
                  autonomyReady={gate.ready}
                  autonomyBlockedReason={gate.blockedReason}
                  onChangeSwitch={handleSwitchChange}
                  onChangeMagnitude={(r, magnitude_pct) => updateRule.mutate({ rule: r, patch: { magnitude_pct } })}
                />
              ))}
            </fieldset>
          )}
        </QueryBoundary>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Guardarraíles por cuenta</h2>
        <QueryBoundary
          isLoading={guardrailsQuery.isLoading}
          isError={guardrailsQuery.isError}
          error={guardrailsQuery.error}
          onRetry={() => void guardrailsQuery.refetch()}
          data={guardrailsQuery.data}
          isEmpty={(data) => data.items.length === 0}
          emptyTitle="Sin guardarraíles"
          emptyBody="Configura los límites duros por cuenta cuando conectes una plataforma."
        >
          {(data) =>
            data.items.map((guardrail) => (
              <GuardrailCard
                key={guardrail.guardrail_id}
                guardrail={guardrail}
                busy={updateGuardrail.isPending}
                refreshing={guardrailsQuery.isFetching}
                onSave={(update, requiresConfirm) => {
                  if (requiresConfirm) setPendingGuardrail({ guardrailId: guardrail.guardrail_id, update });
                  else updateGuardrail.mutate({ guardrailId: guardrail.guardrail_id, update });
                }}
              />
            ))
          }
        </QueryBoundary>
      </section>

      {pendingGuardrail ? (
        <TypedConfirmDialog
          title="Subir un límite de guardarraíl"
          description="Vas a subir un tope o techo. Escribe SUBIR para confirmar."
          confirmLabel="Subir"
          confirmWord="SUBIR"
          danger
          onConfirm={async () => {
            await updateGuardrail.mutateAsync(pendingGuardrail);
            setPendingGuardrail(null);
          }}
          onClose={() => setPendingGuardrail(null)}
        />
      ) : null}

      {brakeDialogOpen ? (
        <BrakeDialog
          killSwitch={killSwitch.data}
          onEngage={handleEngageBrake}
          onRelease={async (item, reason) => {
            await setKillSwitch.mutateAsync({
              scope_kind: item.scope_kind,
              scope_id: item.scope_id,
              mode: item.mode,
              engaged: false,
              reason: reason || "Reactivado desde Reglas",
              typed_confirmation: "REACTIVAR",
            });
            setBrakeDialogOpen(false);
          }}
          onClose={() => setBrakeDialogOpen(false)}
        />
      ) : null}
    </div>
  );
}
