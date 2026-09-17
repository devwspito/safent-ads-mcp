import { useState, type FormEvent } from "react";
import { ApiRequestError } from "@/api/client";
import { useCloudflareConnection, useConnectCloudflareToken, useDisconnectCloudflareToken } from "@/api/queries/cloudflare";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { describeApiError } from "@/utils/apiError";
import styles from "./CloudflareConnectionCard.module.css";

const ACCOUNT_ID_PATTERN = /^[0-9a-f]{32}$/;
const PROFILE_TOKENS_URL = "https://dash.cloudflare.com/profile/api-tokens";

function createTokenUrlFor(accountId: string, fallback: string): string {
  const trimmed = accountId.trim().toLowerCase();
  return ACCOUNT_ID_PATTERN.test(trimmed) ? `https://dash.cloudflare.com/${trimmed}/api-tokens` : fallback;
}

/**
 * Tarjeta "Cloudflare" (lane 006-cloudflare-ui, owner decision: "crear una
 * conexión con Cloudflare pidiendo el token e indicando el enlace donde
 * crearlo"). Mismo lugar que las credenciales de desarrollador de Google/Meta
 * -- el propietario teclea un único token por instalación, nunca por chat ni
 * por variable de entorno. El campo de token nunca se precarga ni se
 * muestra: se limpia al guardar y al fallar.
 *
 * El formulario (y su vocabulario técnico, "token") nunca es la vista por
 * defecto -- design.md §0.10/§13: "cero palabras técnicas" en lo visible de
 * Ajustes. Un botón lo revela, igual criterio de revelación explícita que
 * "Ver configuración técnica"/"Reemplazar" en `DeveloperCredentialsCard`.
 */
export function CloudflareConnectionCard() {
  const connectionQuery = useCloudflareConnection();
  const connect = useConnectCloudflareToken();
  const disconnect = useDisconnectCloudflareToken();
  const status = connectionQuery.data;

  const [showForm, setShowForm] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [token, setToken] = useState("");
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const createTokenUrl = status ? createTokenUrlFor(accountId, status.create_token_url) : PROFILE_TOKENS_URL;

  function startReplacing() {
    setAccountId(status?.account_id ?? "");
    setErrorMessage(null);
    setShowForm(true);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    try {
      await connect.mutateAsync({ token, account_id: accountId.trim() || undefined });
      setShowForm(false);
    } catch (error) {
      setErrorMessage(error instanceof ApiRequestError ? error.message : "No se pudo conectar Cloudflare.");
    } finally {
      setToken("");
    }
  }

  async function handleDisconnect() {
    await disconnect.mutateAsync();
    setConfirmingDisconnect(false);
  }

  return (
    <div className={styles.card}>
      <div className={styles.header}>
        <span className={styles.title}>Cloudflare</span>
        {connectionQuery.isLoading ? null : (
          <span className={styles.status} role="status">
            {status?.connected ? (status.zones.length > 0 ? `Conectado a ${status.zones.join(", ")}` : "Conectado") : "No conectado"}
          </span>
        )}
      </div>

      {connectionQuery.isError ? (
        <p role="alert" className={styles.error}>
          {describeApiError(connectionQuery.error)}
        </p>
      ) : null}

      {status?.connected ? (
        <dl className={styles.summary}>
          {status.account_id ? (
            <div>
              <dt>Cuenta</dt>
              <dd>{status.account_id}</dd>
            </div>
          ) : null}
          <div>
            <dt>Zonas</dt>
            <dd>{status.zones.length > 0 ? status.zones.join(", ") : "Ninguna todavía"}</dd>
          </div>
        </dl>
      ) : null}

      {status && !status.connected && !showForm ? (
        <button type="button" className={styles.button} onClick={() => setShowForm(true)}>
          Conectar Cloudflare
        </button>
      ) : null}

      {status?.connected && !showForm ? (
        <div className={styles.actions}>
          <button type="button" className={styles.button} onClick={startReplacing}>
            Reemplazar
          </button>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonDanger}`}
            onClick={() => setConfirmingDisconnect(true)}
            disabled={disconnect.isPending}
          >
            Desconectar
          </button>
        </div>
      ) : null}

      {showForm ? (
        <form className={styles.form} onSubmit={(event) => void handleSubmit(event)}>
          <p className={styles.label}>
            Necesitas un token de Cloudflare con los permisos <code>Zone.Read</code> y <code>DNS.Edit</code>.
            Elige la plantilla «Editar DNS de zona» y restringe el token a la zona que quieras administrar.
          </p>
          <a href={createTokenUrl} target="_blank" rel="noreferrer" className={styles.link}>
            Crear token en Cloudflare
          </a>

          <label className={styles.label} htmlFor="cloudflare-token">
            Token de Cloudflare
          </label>
          <div className={styles.row}>
            <input
              id="cloudflare-token"
              className={styles.input}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="Pega aquí el token"
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
            <button
              type="submit"
              className={styles.button}
              aria-label="Conectar Cloudflare"
              disabled={!token.trim() || connect.isPending}
            >
              {connect.isPending ? "Conectando…" : "Conectar"}
            </button>
            <button type="button" className={styles.button} onClick={() => setShowForm(false)}>
              Cancelar
            </button>
          </div>
          <p className={styles.label}>Pega el token. Si es un token de cuenta, detectamos la cuenta solos.</p>

          <details className={styles.options} open={accountId.trim().length > 0}>
            <summary>Opciones</summary>
            <label className={styles.label} htmlFor="cloudflare-account-id">
              Identificador de cuenta (opcional)
            </label>
            <input
              id="cloudflare-account-id"
              className={styles.input}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="32 caracteres, lo indica el panel de Cloudflare"
              value={accountId}
              onChange={(event) => setAccountId(event.target.value)}
            />
          </details>
          {errorMessage ? (
            <span className={styles.error} role="alert">
              {errorMessage}
            </span>
          ) : null}
        </form>
      ) : null}

      {confirmingDisconnect ? (
        <TypedConfirmDialog
          title="Desconectar Cloudflare"
          description="Las herramientas de DNS dejarán de funcionar hasta que vuelvas a conectar un token. Escribe DESCONECTAR para confirmar."
          confirmLabel="Desconectar"
          confirmWord="DESCONECTAR"
          danger
          onConfirm={() => void handleDisconnect()}
          onClose={() => setConfirmingDisconnect(false)}
        />
      ) : null}
    </div>
  );
}
