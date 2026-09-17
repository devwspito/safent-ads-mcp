import { useEffect, useId, useRef, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { ApiRequestError } from "@/api/client";
import { useFederatedStatus, useLogin, useMe, useStartFederatedLogin } from "@/api/queries/auth";
import { describeFederatedError } from "@/features/auth/federatedErrorMessages";
import { navigateTo } from "@/utils/navigation";
import { sanitizeNextPath } from "@/utils/nextPath";
import styles from "./LoginPage.module.css";

/** El `txn` que trajera `?next=/oauth/autorizar?txn=…`, para volver a esa misma transacción. */
function extractTxnId(nextPath: string): string | null {
  const [, query] = nextPath.split("?");
  return query ? new URLSearchParams(query).get("txn") : null;
}

/** Mismo patrón que `stripInitialUrlState` en `ConsentimientoPage`: un solo uso, se borra al leerlo. */
function stripFederatedErrorFromUrl() {
  const url = new URL(window.location.href);
  url.searchParams.delete("federated_error");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

/**
 * `POST /auth/login` → cookie `ads_session` (rest-api.md §Autenticación).
 * Community owner password login; native Safent uses the existing SSO session.
 * `?next=` (contracts/oauth.md §4) vuelve ahí tras entrar — p.ej. la página
 * de consentimiento MCP cuando no había sesión.
 *
 * `GET /auth/federated/status` (contracts/federated-login.md §1) decide si se pinta «Entrar
 * con Google»: 404, error de red o carga en curso ⇒ no se pinta (cerrado por defecto).
 */
export function LoginPage() {
  const { data: me } = useMe();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const login = useLogin();
  const federatedStatus = useFederatedStatus();
  const startFederatedLogin = useStartFederatedLogin();
  const submitting = useRef(false);
  const nextPath = sanitizeNextPath(searchParams.get("next"));

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const emailId = useId();
  const passwordId = useId();

  useEffect(() => {
    const code = searchParams.get("federated_error");
    if (!code) return;
    setFormError(describeFederatedError(code));
    stripFederatedErrorFromUrl();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (me) return <Navigate to={nextPath} replace />;

  async function handleGoogleLogin() {
    setFormError(null);
    try {
      const { authorization_url } = await startFederatedLogin.mutateAsync(extractTxnId(nextPath));
      if (!navigateTo(authorization_url)) {
        setFormError("No se ha podido abrir Google. Vuelve a intentarlo.");
      }
    } catch (error) {
      setFormError(errorMessage(error));
    }
  }

  async function handleCredentialsSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setFormError(null);
    try {
      await login.mutateAsync({ email, password });
      setPassword("");
      navigate(nextPath, { replace: true });
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.card}>
        <h1 className={styles.brand}>Safent Ads</h1>

        {federatedStatus.data?.available ? (
          <>
            <button
              type="button"
              className={styles.googleButton}
              onClick={() => void handleGoogleLogin()}
              disabled={startFederatedLogin.isPending}
            >
              {startFederatedLogin.isPending ? "Abriendo Google…" : "Entrar con Google"}
            </button>
            <div className={styles.divider} role="presentation">
              <span>o</span>
            </div>
          </>
        ) : null}

        <form onSubmit={handleCredentialsSubmit} noValidate>
          <p className={styles.subtitle}>Entra con tu cuenta de propietario.</p>
          {formError ? (
            <p className={styles.formError} role="alert">
              {formError}
            </p>
          ) : null}
          <div className={styles.field}>
            <label className={styles.label} htmlFor={emailId}>
              Correo
            </label>
            <input
              id={emailId}
              className={styles.input}
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className={styles.field} style={{ marginTop: "var(--sp-3)" }}>
            <label className={styles.label} htmlFor={passwordId}>
              Contraseña
            </label>
            <input
              id={passwordId}
              className={styles.input}
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button type="submit" className={styles.submit} style={{ marginTop: "var(--sp-4)" }} disabled={login.isPending}>
            {login.isPending ? "Entrando…" : "Continuar"}
          </button>
        </form>
      </div>
    </div>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.status === 429) return "Demasiados intentos. Espera unos minutos.";
    return error.message;
  }
  return "No se ha podido completar la petición.";
}
