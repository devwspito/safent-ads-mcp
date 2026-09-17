import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { ApiRequestError } from "@/api/client";
import { useMe } from "@/api/queries/auth";
import { useApproveMcpOauthConsent, useDenyMcpOauthConsent, useMcpOauthConsent } from "@/api/queries/mcpOauth";
import type { McpOauthConsentActionResponse } from "@/api/schemas/mcpOauth";
import { FreshIdentificationPrompt } from "@/components/common/FreshIdentificationPrompt";
import { Skeleton } from "@/components/states/Skeleton";
import { describeFederatedError } from "@/features/auth/federatedErrorMessages";
import { useFreshIdentification } from "@/hooks/useFreshIdentification";
import { describeApiError, type ReauthMethod } from "@/utils/apiError";
import { instanceIdentity } from "@/utils/instanceIdentity";
import { describeConsentScope } from "@/utils/mcpOauthScopes";
import { navigateTo } from "@/utils/navigation";
import styles from "./ConsentimientoPage.module.css";

type Stage = "deciding" | "expired" | "redirecting" | "redirect_error";

interface InitialUrlState {
  txnId: string | null;
  federatedErrorCode: string | null;
}

/** Un único parseo de `URLSearchParams` para `txn` y `federated_error` (FR-113, §1 del contrato):
 *  el servidor devuelve al dueño a `/oauth/autorizar?txn=…&federated_error=<código>` cuando el
 *  salto de re-identificación falla y la transacción de consentimiento sigue viva. */
function readInitialUrlState(): InitialUrlState {
  const params = new URLSearchParams(window.location.search);
  return { txnId: params.get("txn"), federatedErrorCode: params.get("federated_error") };
}

/** C-58: la SPA borra `?txn=` y `?federated_error=` de la URL en cuanto los lee, un solo uso. */
function stripInitialUrlState() {
  const url = new URL(window.location.href);
  url.searchParams.delete("txn");
  url.searchParams.delete("federated_error");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 401;
}

/** 404 (desconocido), 409 (ya resuelto) o 410 (caducado) — mismo mensaje para el propietario. */
function isUnavailable(error: unknown): boolean {
  return error instanceof ApiRequestError && [404, 409, 410].includes(error.status);
}

function nextPathFor(txnId: string | null): string {
  const path = txnId ? `/oauth/autorizar?txn=${txnId}` : "/oauth/autorizar";
  return `/login?next=${encodeURIComponent(path)}`;
}

/** Cuerpo del prompt de prueba de presencia — FR-112, contracts/federated-login.md §4. */
function approvePromptDescription(methods: ReauthMethod[]): string {
  if (methods.includes("federated") && !methods.includes("totp")) {
    return "Vuelve a identificarte con Google para continuar.";
  }
  return "Escribe el código de tu aplicación de autenticación para autorizar este acceso.";
}

/** El fallo de la vuelta federada (§1 del contrato, FR-113) puede llegar con la transacción ya
 *  caducada o el consentimiento ya resuelto: se pinta en las tres pantallas donde el dueño puede
 *  aterrizar, nunca solo en la feliz. */
function FederatedErrorAlert({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p className={styles.formError} role="alert">
      {message}
    </p>
  );
}

/**
 * `GET/POST /api/v1/mcp-oauth/consent/{txn}` (contracts/oauth.md §5): pantalla de consentimiento
 * de un cliente MCP (Claude Code, Codex…) pidiendo acceso a esta instancia
 * (`instanceIdentity()`, contracts/instance-identity.d.ts). R-6 del threat-model exige mostrar
 * `client_id` y el host de `redirect_uri` completos, sin truncar, para que un `client_name`
 * engañoso no baste para que el propietario apruebe un cliente ajeno.
 */
export function ConsentimientoPage() {
  const [{ txnId, federatedErrorCode }] = useState(readInitialUrlState);
  const [stage, setStage] = useState<Stage>("deciding");
  const [denyError, setDenyError] = useState<string | null>(null);
  const meQuery = useMe();
  const me = meQuery.data;
  const federatedErrorMessage = federatedErrorCode ? describeFederatedError(federatedErrorCode) : null;

  useEffect(() => {
    if (txnId || federatedErrorCode) stripInitialUrlState();
  }, [txnId, federatedErrorCode]);

  const consentQuery = useMcpOauthConsent(txnId);
  const approveConsent = useApproveMcpOauthConsent(txnId ?? "");
  const denyConsent = useDenyMcpOauthConsent(txnId ?? "");

  function goToRedirect(redirectTo: string) {
    setStage(navigateTo(redirectTo) ? "redirecting" : "redirect_error");
  }

  const approveFreshId = useFreshIdentification<Record<string, never>, McpOauthConsentActionResponse>({
    txnId,
    federatedLoginAvailable: me?.federated_login_available ?? false,
    mutate: async (variables, reauthToken) => {
      try {
        return await approveConsent.mutateAsync({ ...variables, reauthToken });
      } catch (error) {
        if (isUnavailable(error)) setStage("expired");
        throw error;
      }
    },
    onSuccess: (data) => goToRedirect(data.redirect_to),
  });

  async function handleDeny() {
    setDenyError(null);
    try {
      const data = await denyConsent.mutateAsync();
      goToRedirect(data.redirect_to);
    } catch (error) {
      if (isUnavailable(error)) {
        setStage("expired");
      } else {
        setDenyError(describeApiError(error));
      }
    }
  }

  if (!txnId || stage === "expired" || (consentQuery.isError && isUnavailable(consentQuery.error))) {
    return (
      <main className={styles.wrap}>
        <div className={styles.card}>
          <FederatedErrorAlert message={federatedErrorMessage} />
          <p className={styles.body} role="alert">
            Esta solicitud ha caducado. Vuelve a Claude Code o Codex y repite la conexión.
          </p>
        </div>
      </main>
    );
  }

  if (consentQuery.isError && isUnauthorized(consentQuery.error)) {
    return <Navigate to={nextPathFor(txnId)} replace />;
  }

  /** Fallo de `POST /auth/federated/start` en el salto a Google — FR-113, dos salidas explícitas. */
  if (approveFreshId.federatedStartError) {
    return (
      <main className={styles.wrap}>
        <div className={styles.card}>
          <p className={styles.body} role="alert">
            {approveFreshId.federatedStartError}
          </p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondaryButton} onClick={() => void handleDeny()}>
              Cancelar la solicitud
            </button>
            <button type="button" className={styles.primaryButton} onClick={approveFreshId.retryFederatedStart}>
              Volver a intentarlo
            </button>
          </div>
        </div>
      </main>
    );
  }

  if (stage === "redirecting") {
    return (
      <main className={styles.wrap}>
        <div className={styles.card}>
          <p className={styles.body} role="status">
            Listo, vuelve a tu terminal.
          </p>
        </div>
      </main>
    );
  }

  if (stage === "redirect_error") {
    return (
      <main className={styles.wrap}>
        <div className={styles.card}>
          <p className={styles.body} role="alert">
            No hemos podido volver a tu terminal automáticamente. Vuelve a Claude Code o Codex y repite la
            conexión.
          </p>
        </div>
      </main>
    );
  }

  if (consentQuery.isLoading) {
    return (
      <main className={styles.wrap}>
        <div className={styles.card}>
          <div role="status" aria-label="Cargando la solicitud">
            <Skeleton height="160px" />
          </div>
        </div>
      </main>
    );
  }

  if (consentQuery.isError) {
    return (
      <main className={styles.wrap}>
        <div className={styles.card}>
          <FederatedErrorAlert message={federatedErrorMessage} />
          <p className={styles.body} role="alert">
            {describeApiError(consentQuery.error)}
          </p>
          <button type="button" className={styles.secondaryButton} onClick={() => void consentQuery.refetch()}>
            Reintentar
          </button>
        </div>
      </main>
    );
  }

  const consent = consentQuery.data;
  if (!consent) return null;

  return (
    <main className={styles.wrap}>
      <div className={styles.card}>
        <FederatedErrorAlert message={federatedErrorMessage} />

        <h1 className={styles.title}>
          <span className={styles.clientName}>{consent.client_name}</span> quiere acceder a{" "}
          {instanceIdentity().name}
        </h1>

        <dl className={styles.details}>
          <div>
            <dt className={styles.detailLabel}>Identificador de la aplicación</dt>
            <dd className={styles.detailValue}>
              <code>{consent.client_id}</code>
            </dd>
          </div>
          <div>
            <dt className={styles.detailLabel}>Te devolverá a</dt>
            <dd className={styles.detailValue}>
              <code>{consent.redirect_host}</code>
            </dd>
          </div>
        </dl>

        <div>
          <p className={styles.scopesTitle}>Podrá:</p>
          <ul className={styles.scopesList}>
            {consent.scopes.map((scope) => (
              <li key={scope.name}>{describeConsentScope(scope)}</li>
            ))}
          </ul>
        </div>

        {denyError ? (
          <p className={styles.formError} role="alert">
            {denyError}
          </p>
        ) : null}

        {approveFreshId.errorMessage && !approveFreshId.isPromptOpen ? (
          <p className={styles.formError} role="alert">
            {approveFreshId.errorMessage}
          </p>
        ) : null}

        <div className={styles.actions}>
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => void handleDeny()}
            disabled={denyConsent.isPending}
          >
            {denyConsent.isPending ? "Cancelando…" : "Cancelar"}
          </button>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={() => approveFreshId.start({})}
            disabled={approveFreshId.isSubmitting || denyConsent.isPending || !meQuery.isSuccess}
          >
            Autorizar
          </button>
        </div>
      </div>

      {approveFreshId.isPromptOpen ? (
        <FreshIdentificationPrompt
          title="Confirmar acceso"
          description={approvePromptDescription(approveFreshId.methods)}
          confirmLabel="Autorizar"
          methods={approveFreshId.methods}
          freshUntil={me?.session?.fresh_identification_until}
          isSubmitting={approveFreshId.isSubmitting}
          isStartingGoogle={approveFreshId.isStartingGoogle}
          errorMessage={approveFreshId.errorMessage}
          onConfirmTotp={approveFreshId.confirm}
          onConfirmWithGoogle={approveFreshId.confirmWithGoogle}
          onClose={approveFreshId.cancel}
        />
      ) : null}
    </main>
  );
}
