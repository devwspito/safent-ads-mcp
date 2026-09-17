import { useState, type FormEvent } from "react";
import { useSetGoogleAppCredentials, useSetMetaAppCredentials } from "@/api/queries/platformApps";
import type { Platform } from "@/api/schemas";
import type { PlatformAppStatus, SetGoogleAppCredentialsInput, SetMetaAppCredentialsInput } from "@/api/schemas/platformApps";
import { ActionConfirmationDialog } from "@/components/common/ActionConfirmationDialog";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { useConfirmedMutation } from "@/hooks/useConfirmedMutation";
import { platformLabel } from "@/utils/platform";
import { formatRelativeTime } from "@/utils/time";
import { defaultGoogleClientType } from "./googleClientDefault";
import styles from "./DeveloperCredentialsCard.module.css";

interface DeveloperCredentialsCardProps {
  platform: Platform;
  status: PlatformAppStatus | undefined;
  isLoading: boolean;
  onDelete: (platform: Platform) => void;
  isDeleting: boolean;
}

interface GoogleFormState {
  clientType: "web" | "desktop";
  clientId: string;
  clientSecret: string;
  loginCustomerId: string;
}

interface MetaFormState {
  appId: string;
  appSecret: string;
}

const emptyGoogleForm = (): GoogleFormState => ({
  clientType: defaultGoogleClientType(window.location.origin),
  clientId: "",
  clientSecret: "",
  loginCustomerId: "",
});
const EMPTY_META_FORM: MetaFormState = { appId: "", appSecret: "" };

function isGoogleFormComplete(form: GoogleFormState): boolean {
  return Boolean(form.clientId.trim() && (form.clientType === "desktop" || form.clientSecret.trim()));
}

function isMetaFormComplete(form: MetaFormState): boolean {
  return Boolean(form.appId.trim() && form.appSecret.trim());
}

/**
 * Tarjeta "Credenciales de desarrollador" por plataforma (owner decision,
 * app-credentials-ui): estado, ids enmascarados, la redirect URI a registrar en
 * la consola del proveedor con botón de copiar, alta/reemplazo (nunca precargado,
 * confirmación del contenido exacto) y borrado con confirmación tecleada. El botón
 * "Conectar" de `ConnectProviderCard` queda deshabilitado hasta que `configured`
 * sea `true` aquí.
 */
export function DeveloperCredentialsCard({
  platform,
  status,
  isLoading,
  onDelete,
  isDeleting,
}: DeveloperCredentialsCardProps) {
  const [showForm, setShowForm] = useState(false);
  const [googleForm, setGoogleForm] = useState<GoogleFormState>(emptyGoogleForm);
  const [metaForm, setMetaForm] = useState<MetaFormState>(EMPTY_META_FORM);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [copied, setCopied] = useState(false);

  const setGoogle = useSetGoogleAppCredentials();
  const setMeta = useSetMetaAppCredentials();

  const googleConfirmation = useConfirmedMutation<SetGoogleAppCredentialsInput, unknown>({
    scopeKey: "platform-apps:google",
    summarize: (v) => [`Google · Client ID: ${v.client_id}`, `Tipo: ${v.client_type === "desktop" ? "Escritorio · PKCE sin secreto" : "Aplicación web"}`, `MCC: ${v.login_customer_id || "Sin MCC"}`, ...(v.client_type === "desktop" ? [] : ["Client secret: valor nuevo oculto"])],
    mutate: (variables) => setGoogle.mutateAsync(variables),
    onSuccess: () => {
      setGoogleForm(emptyGoogleForm());
      setShowForm(false);
    },
  });
  const metaConfirmation = useConfirmedMutation<SetMetaAppCredentialsInput, unknown>({
    scopeKey: "platform-apps:meta",
    summarize: (v) => [`Meta · App ID: ${v.app_id}`, "App secret: valor nuevo oculto"],
    mutate: (variables) => setMeta.mutateAsync(variables),
    onSuccess: () => {
      setMetaForm(EMPTY_META_FORM);
      setShowForm(false);
    },
  });
  const confirmation = platform === "google" ? googleConfirmation : metaConfirmation;
  const formIsComplete = platform === "google" ? isGoogleFormComplete(googleForm) : isMetaFormComplete(metaForm);
  // Before saving (and while replacing), the visible form is the source of
  // guidance. A missing persisted client_type must not label Desktop as Web.
  const displayedClientType = !status?.configured || showForm
    ? googleForm.clientType
    : (status.client_type ?? "web");

  function startSaving(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (platform === "google") {
      googleConfirmation.start({
        client_id: googleForm.clientId,
        client_type: googleForm.clientType,
        ...(googleForm.clientType === "web" ? {client_secret: googleForm.clientSecret} : {}),
        login_customer_id: googleForm.loginCustomerId.trim() || undefined,
      });
    } else {
      metaConfirmation.start({ app_id: metaForm.appId, app_secret: metaForm.appSecret });
    }
  }

  async function copyRedirectUri() {
    if (!status) return;
    await navigator.clipboard.writeText(status.redirect_uri);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className={styles.card}>
      <div className={styles.header}>
        <span className={styles.title}>{platformLabel(platform)}</span>
        {isLoading ? null : (
          <span className={styles.status} role="status">
            {status?.configured
              ? `Configurada${status.updated_at ? ` · ${formatRelativeTime(status.updated_at)}` : ""}`
              : "Sin configurar"}
          </span>
        )}
      </div>

      {status?.configured ? (
        <dl className={styles.summary}>
          {platform === "google" ? <div><dt>Tipo de cliente OAuth</dt><dd>{status.client_type === "desktop" ? "Escritorio · PKCE" : "Aplicación web"}</dd></div> : null}
          <div>
            <dt>Identificador de cliente</dt>
            <dd>{status.client_id_masked}</dd>
          </div>
          {status.login_customer_id_masked ? (
            <div>
              <dt>Cuenta de gestor (MCC)</dt>
              <dd>{status.login_customer_id_masked}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      {status ? (
        <div className={styles.redirectRow}>
          <span className={styles.label}>{platform === "google"
            ? displayedClientType === "desktop"
              ? "Retorno local de escritorio (puerto dinámico; no se registra manualmente)"
              : "URI de retorno para registrar en el cliente web de Google"
            : "URI de retorno que debe admitir tu aplicación Meta"}</span>
          <div className={styles.row}>
            <code className={styles.redirectUri}>{status.redirect_uri}</code>
            <button type="button" className={styles.button} onClick={() => void copyRedirectUri()}>
              {copied ? "Copiado" : "Copiar"}
            </button>
          </div>
        </div>
      ) : null}
      {platform === "meta" ? <p className={styles.label}>El App Secret pertenece a tu aplicación Meta, no es tu contraseña. Comprueba que la app admite el retorno mostrado; el retorno HTTP local con puerto dinámico no está verificado para Meta.</p> : null}

      {status?.configured && !showForm ? (
        <div className={styles.actions}>
          <button type="button" className={styles.button} onClick={() => setShowForm(true)}>
            Reemplazar
          </button>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonDanger}`}
            onClick={() => setConfirmingDelete(true)}
            disabled={isDeleting}
          >
            Eliminar
          </button>
        </div>
      ) : null}

      {!status?.configured || showForm ? (
        <form className={styles.form} onSubmit={startSaving}>
          {platform === "google" ? (
            <>
              <label className={styles.label} htmlFor="google-client-type">Tipo de cliente OAuth</label>
              <select
                id="google-client-type"
                className={styles.input}
                value={googleForm.clientType}
                aria-describedby="google-client-type-help"
                onChange={(event) => setGoogleForm({...googleForm, clientType: event.target.value as "web" | "desktop", clientSecret: ""})}
              >
                <option value="web">Aplicación web</option>
                <option value="desktop">Aplicación de escritorio (Safent nativo)</option>
              </select>
              <p id="google-client-type-help" className={styles.label}>
                {googleForm.clientType === "desktop"
                  ? "Crea un cliente de tipo Aplicación de escritorio en Google Cloud. Usa PKCE y retorno local; no necesitas un secreto de cliente."
                  : "El cliente web requiere secreto y la URI exacta registrada en Google Cloud."}
              </p>
              <label className={styles.label} htmlFor={`${platform}-client-id`}>
                Client ID
              </label>
              <input
                id={`${platform}-client-id`}
                className={styles.input}
                type="text"
                autoComplete="off"
                placeholder="xxxxx.apps.googleusercontent.com"
                value={googleForm.clientId}
                onChange={(event) => setGoogleForm({ ...googleForm, clientId: event.target.value })}
              />
              {googleForm.clientType === "web" ? <><label className={styles.label} htmlFor={`${platform}-client-secret`}>
                Client secret
              </label>
              <input
                id={`${platform}-client-secret`}
                className={styles.input}
                type="password"
                autoComplete="off"
                value={googleForm.clientSecret}
                onChange={(event) => setGoogleForm({ ...googleForm, clientSecret: event.target.value })}
              />
              </> : null}
              <label className={styles.label} htmlFor={`${platform}-login-customer-id`}>
                Cuenta de gestor (MCC), opcional
              </label>
              <input
                id={`${platform}-login-customer-id`}
                className={styles.input}
                type="text"
                autoComplete="off"
                value={googleForm.loginCustomerId}
                onChange={(event) => setGoogleForm({ ...googleForm, loginCustomerId: event.target.value })}
              />
            </>
          ) : (
            <>
              <label className={styles.label} htmlFor={`${platform}-app-id`}>
                App ID
              </label>
              <input
                id={`${platform}-app-id`}
                className={styles.input}
                type="text"
                autoComplete="off"
                value={metaForm.appId}
                onChange={(event) => setMetaForm({ ...metaForm, appId: event.target.value })}
              />
              <label className={styles.label} htmlFor={`${platform}-app-secret`}>
                App secret
              </label>
              <input
                id={`${platform}-app-secret`}
                className={styles.input}
                type="password"
                autoComplete="off"
                value={metaForm.appSecret}
                onChange={(event) => setMetaForm({ ...metaForm, appSecret: event.target.value })}
              />
            </>
          )}
          <div className={styles.actions}>
            <button type="submit" className={styles.button} disabled={!formIsComplete || confirmation.isSubmitting}>
              Guardar
            </button>
            {status?.configured ? (
              <button type="button" className={styles.button} onClick={() => setShowForm(false)}>
                Cancelar
              </button>
            ) : null}
          </div>
        </form>
      ) : null}

      {confirmation.isPromptOpen ? (
        <ActionConfirmationDialog
          title={`Confirmar credenciales de ${platformLabel(platform)}`}
          description="Se guardarán las credenciales indicadas. Los secretos no se mostrarán."
          confirmLabel="Guardar"
          summary={confirmation.summary}
          canConfirm={confirmation.canConfirm}
          isSubmitting={confirmation.isSubmitting}
          errorMessage={confirmation.errorMessage}
          onConfirm={confirmation.confirm}
          onClose={confirmation.cancel}
        />
      ) : null}

      {confirmingDelete ? (
        <TypedConfirmDialog
          title={`Eliminar credenciales de ${platformLabel(platform)}`}
          description="El propietario no podrá conectar ni reconectar cuentas de esta plataforma hasta volver a configurarlas. Escribe ELIMINAR para confirmar."
          confirmLabel="Eliminar"
          confirmWord="ELIMINAR"
          danger
          onConfirm={() => {
            onDelete(platform);
            setConfirmingDelete(false);
          }}
          onClose={() => setConfirmingDelete(false)}
        />
      ) : null}
    </div>
  );
}
