import { useCallback, useRef, useState, type MouseEvent } from "react";
import { useMe } from "@/api/queries/auth";
import { useMcpOauthGrants, useRevokeMcpOauthGrant } from "@/api/queries/mcpOauth";
import type { McpOauthGrant } from "@/api/schemas/mcpOauth";
import { ActionConfirmationDialog } from "@/components/common/ActionConfirmationDialog";
import { FreshIdentificationPrompt } from "@/components/common/FreshIdentificationPrompt";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { useRevokeGrantFlow } from "@/hooks/useRevokeGrantFlow";
import type { ReauthMethod } from "@/utils/apiError";
import { describeGrantScope } from "@/utils/mcpOauthScopes";
import { formatExactDate, formatRelativeTime } from "@/utils/time";
import styles from "./ConnectedAgentsSection.module.css";

/** Cuerpo del prompt de prueba de presencia — contracts/federated-login.md §4. */
function revokePromptDescription(methods: ReauthMethod[], agentName: string): string {
  if (methods.includes("federated") && !methods.includes("totp")) {
    return "Vuelve a identificarte con Google para continuar.";
  }
  return `Escribe el código de tu aplicación de autenticación para quitar el acceso de ${agentName}.`;
}

/**
 * `GET/POST /api/v1/mcp-oauth/grants*` (contracts/oauth.md §5, tasks.md T015b/T017b, C-55,
 * contracts/federated-login.md §2 y §4): los clientes MCP (Claude Code, Codex…) que el
 * propietario ya autorizó, con un botón para cortarles el acceso al instante. "Quitar acceso"
 * encadena dos diálogos (`useRevokeGrantFlow`): primero la prueba de presencia fresca (Google o
 * TOTP, igual que el resto de mutaciones sensibles del panel), y solo con eso resuelto, la
 * revisión de la acción exacta (428 `CONFIRMATION_REQUIRED`) antes de revocar la familia entera
 * en el servidor. El foco vuelve al botón «Quitar acceso» que abrió la cadena al cerrar
 * cualquiera de los dos diálogos (WCAG 2.4.3) — no a "quien tuviera el foco" en ese instante,
 * que tras el primer diálogo ya no sería ese botón.
 */
export function ConnectedAgentsSection() {
  const meQuery = useMe();
  const grantsQuery = useMcpOauthGrants();
  const revokeGrant = useRevokeMcpOauthGrant();
  const [revokingAgentName, setRevokingAgentName] = useState<string | null>(null);
  const revokeButtonRef = useRef<HTMLButtonElement | null>(null);

  const revoke = useCallback(
    (variables: { grantId: string; reauthToken?: string; confirmationToken?: string }) =>
      revokeGrant.mutateAsync(variables),
    [revokeGrant],
  );

  const flow = useRevokeGrantFlow({
    revoke,
    federatedLoginAvailable: meQuery.data?.federated_login_available ?? false,
    onRevoked: () => setRevokingAgentName(null),
  });

  function startRevoke(grant: McpOauthGrant, event: MouseEvent<HTMLButtonElement>) {
    revokeButtonRef.current = event.currentTarget;
    setRevokingAgentName(grant.client_name);
    flow.presence.start({ grantId: grant.grant_id });
  }

  function closePresencePrompt() {
    flow.presence.cancel();
    setRevokingAgentName(null);
  }

  function closeConfirmationDialog() {
    flow.cancelConfirmation();
    setRevokingAgentName(null);
  }

  const isBusy = flow.presence.isSubmitting || flow.isConfirmationOpen || meQuery.isLoading;
  // Un 5xx (o una identificación fresca perdida) en la llamada final puede cerrar el segundo
  // diálogo sin que `confirmError` se muestre dentro de él — sin esto, el clic "no hace nada".
  const outsideDialogError =
    flow.presence.errorMessage && !flow.presence.isPromptOpen
      ? flow.presence.errorMessage
      : flow.confirmError && !flow.isConfirmationOpen
        ? flow.confirmError
        : null;

  return (
    <>
      <QueryBoundary
        isLoading={grantsQuery.isLoading}
        isError={grantsQuery.isError}
        error={grantsQuery.error}
        onRetry={() => void grantsQuery.refetch()}
        data={grantsQuery.data}
        isEmpty={(data) => data.grants.length === 0}
        emptyTitle="Ninguna aplicación con acceso"
        emptyBody="Conecta Claude Code o Codex con el instalador del MCP."
      >
        {(data) => (
          <ul className={styles.list}>
            {data.grants.map((grant) => (
              <li key={grant.grant_id} className={styles.row}>
                <div className={styles.info}>
                  <span className={styles.name}>{grant.client_name}</span>
                  <span className={styles.meta}>
                    <code>{grant.redirect_host}</code>
                  </span>
                  <span className={styles.meta}>{grant.scopes.map(describeGrantScope).join(" · ")}</span>
                  <span className={styles.meta}>
                    Desde {formatExactDate(grant.created_at)} · último uso{" "}
                    {grant.last_used_at ? formatRelativeTime(grant.last_used_at) : "nunca"} · caduca{" "}
                    {grant.expires_at ? formatExactDate(grant.expires_at) : "sin caducidad"}
                  </span>
                </div>
                <button
                  type="button"
                  className={styles.button}
                  onClick={(event) => startRevoke(grant, event)}
                  disabled={isBusy}
                >
                  Quitar acceso
                </button>
              </li>
            ))}
          </ul>
        )}
      </QueryBoundary>

      {outsideDialogError ? (
        <p className={styles.formError} role="alert">
          {outsideDialogError}
        </p>
      ) : null}

      {flow.presence.isPromptOpen ? (
        <FreshIdentificationPrompt
          title="Confirmar quitar acceso"
          description={revokePromptDescription(flow.presence.methods, revokingAgentName ?? "esta aplicación")}
          confirmLabel="Continuar"
          methods={flow.presence.methods}
          freshUntil={meQuery.data?.session?.fresh_identification_until}
          isSubmitting={flow.presence.isSubmitting}
          isStartingGoogle={flow.presence.isStartingGoogle}
          errorMessage={flow.presence.errorMessage}
          onConfirmTotp={flow.presence.confirm}
          onConfirmWithGoogle={flow.presence.confirmWithGoogle}
          onClose={closePresencePrompt}
          returnFocusTo={revokeButtonRef}
        />
      ) : null}

      {flow.isConfirmationOpen ? (
        <ActionConfirmationDialog
          title="Revisa y confirma esta acción"
          description={`Se quitará el acceso de ${revokingAgentName ?? "esta aplicación"}. Tendrá que conectarse de nuevo para volver a usarlo.`}
          confirmLabel="Quitar acceso"
          summary={[revokingAgentName ?? "Esta aplicación"]}
          canConfirm={!flow.confirmSubmitting}
          isSubmitting={flow.confirmSubmitting}
          errorMessage={flow.confirmError}
          onConfirm={flow.confirm}
          onClose={closeConfirmationDialog}
          returnFocusTo={revokeButtonRef}
        />
      ) : null}
    </>
  );
}
