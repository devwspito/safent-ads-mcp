import { useState, type FormEvent } from "react";
import { ApiRequestError } from "@/api/client";
import { useRegisterMetaSystemUserToken } from "@/api/queries/connections";
import styles from "./MetaSystemUserTokenForm.module.css";

interface MetaSystemUserTokenFormProps {
  businessId: string;
}

const DEFAULT_ERROR_MESSAGE = "No se pudo validar el token.";

/**
 * Vía alternativa de Meta para negocios con System User de empresa (rest-api.md §Conexiones):
 * sin `state`/PKCE, el bróker valida el token pegado contra la plataforma antes de crear
 * ninguna cuenta. El campo va enmascarado (`type="password"`), nunca se registra en consola
 * ni vuelve en la respuesta, y se limpia tanto al enviar como al fallar la validación.
 */
export function MetaSystemUserTokenForm({ businessId }: MetaSystemUserTokenFormProps) {
  const [token, setToken] = useState("");
  const [connectedCount, setConnectedCount] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const registerToken = useRegisterMetaSystemUserToken(businessId);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setConnectedCount(null);
    try {
      const result = await registerToken.mutateAsync(token);
      setConnectedCount(result.accounts.length);
    } catch (error) {
      setErrorMessage(error instanceof ApiRequestError ? error.message : DEFAULT_ERROR_MESSAGE);
    } finally {
      setToken("");
    }
  }

  return (
    <form className={styles.form} onSubmit={(event) => void handleSubmit(event)}>
      <label className={styles.label} htmlFor="meta-system-user-token">
        Token de sistema de Meta Business
      </label>
      <div className={styles.row}>
        <input
          id="meta-system-user-token"
          className={styles.input}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="Pega aquí el token del System User"
        />
        <button type="submit" className={styles.button} disabled={!token.trim() || registerToken.isPending}>
          {registerToken.isPending ? "Validando…" : "Usar token de sistema"}
        </button>
      </div>
      {connectedCount !== null ? (
        <span className={styles.status} role="status">
          {connectedCount} cuenta{connectedCount === 1 ? "" : "s"} conectada{connectedCount === 1 ? "" : "s"}.
        </span>
      ) : null}
      {errorMessage ? (
        <span className={styles.error} role="alert">
          {errorMessage}
        </span>
      ) : null}
    </form>
  );
}
