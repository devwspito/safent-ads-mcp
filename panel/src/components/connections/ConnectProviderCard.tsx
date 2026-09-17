import { useEffect, useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiRequestError } from "@/api/client";
import { useReconnectStatus, useStartReconnect } from "@/api/queries/connections";
import type { Platform } from "@/api/schemas";
import { platformLabel } from "@/utils/platform";
import { isReconnectSessionId, readReconnectSession, reconnectSessionKey, saveReconnectSession } from "@/utils/reconnectSession";
import styles from "./ConnectProviderCard.module.css";

interface ConnectProviderCardProps {
  provider: Platform;
  ownerId: string;
  businessId: string;
  googleCustomerIdRequired?: boolean;
  /** `false` hasta que `GET /platform-apps` reporte credenciales de desarrollador guardadas. */
  configured: boolean;
  configurationPending?: boolean;
  configurationError?: boolean;
  onSetup?: () => void;
  guidedSetup?: boolean;
}

const DEFAULT_ERROR_MESSAGE = "No se pudo completar la conexión.";
/** `dispatcher.py:_KNOWN_ERROR_CODES` → `GOOGLE_PROJECT_ACCESS_LEVEL_TEST` (commit e73e929): el proyecto
 * de Google Cloud del cliente OAuth solo tiene acceso de prueba a Google Ads. La acción real vive en la
 * consola del proveedor, no en este panel — enlace directo en vez de obligar a leer la URL en el mensaje. */
const GOOGLE_CLOUD_ACCESS_ERROR_CODE = "GOOGLE_PROJECT_ACCESS_LEVEL_TEST";
const GOOGLE_CLOUD_ADS_API_CONSOLE_URL = "https://console.cloud.google.com/google/ads-apis/overview";

function safeAuthorizationUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

type NativeInvoke = (command: string, args: { url: string }) => Promise<unknown>;

function nativeOAuthOpener(): ((url: string) => Promise<unknown>) | null {
  // Ads is a same-origin iframe inside the native shell. A browser embed may
  // have a cross-origin parent; it keeps its normal HTTPS link behavior.
  try {
    const host = window.parent as Window & { __TAURI__?: { core?: { invoke?: NativeInvoke } } };
    const invoke = host.__TAURI__?.core?.invoke;
    return typeof invoke === "function" ? (url) => Promise.resolve().then(() => invoke("open_ads_oauth", { url })) : null;
  } catch { return null; }
}

function startFailureMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.code === "GOOGLE_ACCOUNT_SELECTION_REQUIRED") return "Introduce el número de cuenta de Google Ads: 10 dígitos, por ejemplo 123-456-7890.";
    if (error.status === 401) return "Tu sesión ha caducado. Vuelve a entrar antes de conectar.";
    if (error.status === 403) return "No se pudo verificar el permiso para conectar. Revisa tu sesión.";
    if (error.status === 429) return "Hay demasiados intentos seguidos. Espera un momento antes de volver a conectar.";
  }
  return "No se pudo iniciar la conexión. Comprueba tu conexión y la preparación de Safent antes de volver a intentarlo.";
}

function pollingFailureMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    if (error.status === 401) return "Tu sesión ha caducado. Vuelve a entrar para comprobar el resultado.";
    if (error.status === 403) return "No se pudo verificar tu permiso para consultar este intento. Revisa tu sesión.";
    if (error.status === 404 || error.status === 410) return "Este intento ya no está disponible. Puedes iniciar una conexión nueva.";
  }
  return "No se pudo comprobar el resultado. La autorización puede haberse completado; reintenta la comprobación.";
}

/**
 * "Conectar" abre `authorize_url` en una pestaña nueva y sondea el desenlace por PLATAFORMA
 * (rest-api.md §Conexiones): el propietario autoriza, no una cuenta concreta, así que este
 * botón no cuelga de ninguna `AccountCard`. Al resolverse en "ok" se refresca la lista de
 * cuentas para que las descubiertas por el bróker aparezcan. Sin configuración,
 * ofrece la guía antes de iniciar OAuth; guardar una aplicación no conecta cuentas.
 */
export function ConnectProviderCard(props: ConnectProviderCardProps) {
  // Remount synchronously on scope changes: never poll the old session under
  // the new owner/business/provider, even for the render before an effect runs.
  return <ScopedConnectProviderCard key={JSON.stringify([props.ownerId, props.businessId, props.provider])} {...props} />;
}

function ScopedConnectProviderCard({ provider, ownerId, businessId, configured, googleCustomerIdRequired = false,
  configurationPending = false, configurationError = false, onSetup, guidedSetup = false }: ConnectProviderCardProps) {
  const storageKey = reconnectSessionKey(ownerId, businessId, provider);
  const [sessionId, setSessionId] = useState<string | null>(() => readReconnectSession(storageKey));
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [storageUnavailable, setStorageUnavailable] = useState(false);
  const [googleCustomerId, setGoogleCustomerId] = useState("");
  const customerInputId = useId();
  const inFlight = useRef(false);
  const context = useRef(0);
  const queryClient = useQueryClient();
  const startReconnect = useStartReconnect(businessId);
  const status = useReconnectStatus(businessId, provider, sessionId, ownerId);
  const state = status.data?.state;
  const sessionUnavailable = status.error instanceof ApiRequestError && [404, 410].includes(status.error.status);
  const waitingForConsent = sessionId !== null && (!state || state === "waiting") && !sessionUnavailable;
  const asksForGoogleAccount = provider === "google" && authorizeUrl !== null &&
    ["connect.composio.dev", "backend.composio.dev", "dashboard.composio.dev"].includes(new URL(authorizeUrl).hostname);
  const needsSetup = !configured && !configurationPending && !configurationError;
  const providerName = provider === "google" ? "Google" : "Meta";

  useEffect(() => {
    context.current += 1;
    return () => { context.current += 1; };
  }, []);

  const { refetch } = status;
  useEffect(() => {
    if (!sessionId || state === "ok" || state === "error") return;
    const check = () => {
      if (document.visibilityState !== "hidden") void refetch({ cancelRefetch: false });
    };
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, [sessionId, state, refetch]);

  useEffect(() => {
    if (state === "ok" && !status.isError) {
      void queryClient.invalidateQueries({ queryKey: ["platform-accounts", businessId] });
    }
  }, [state, status.isError, queryClient, businessId]);

  async function handleConnect() {
    if (inFlight.current || !configured || !ownerId || !businessId || waitingForConsent) return;
    const customerInput = googleCustomerId.trim();
    if (provider === "google" && ((googleCustomerIdRequired && !customerInput) ||
      (customerInput && !/^(?:[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4})$/.test(customerInput)))) {
      setStartError("Introduce el número de cuenta de Google Ads: 10 dígitos, por ejemplo 123-456-7890.");
      return;
    }
    inFlight.current = true;
    const startedContext = context.current;
    setStartError(null);
    setAuthorizeUrl(null);
    setSessionId(null);
    setStorageUnavailable(false);
    try {
      const result = await startReconnect.mutateAsync({ provider, googleCustomerId: customerInput.replaceAll("-", "") || undefined });
      if (context.current !== startedContext) return;
      const url = safeAuthorizationUrl(result.authorize_url);
      if (!url || !isReconnectSessionId(result.session_id)) {
        setStartError("No se recibió un enlace seguro para autorizar la cuenta. Revisa la preparación de la conexión.");
        return;
      }
      setStorageUnavailable(!saveReconnectSession(storageKey, result.session_id));
      setSessionId(result.session_id);
      setAuthorizeUrl(url);
      // The fixed native command validates the target and opens the system
      // browser. Browser-only sessions retain window.open plus a normal link.
      try {
        const openNative = nativeOAuthOpener();
        if (openNative) await openNative(url);
        else window.open(url, "_blank", "noopener,noreferrer");
      } catch {
        if (context.current === startedContext) setStartError("No pudimos abrir el navegador. Usa el enlace para continuar.");
      }
    } catch (error) {
      if (context.current === startedContext) setStartError(startFailureMessage(error));
    } finally {
      if (context.current === startedContext) inFlight.current = false;
    }
  }

  return (
    <div className={styles.card}>
      <span className={styles.label}>{platformLabel(provider)}</span>
      {provider === "google" && configured ? <div className={styles.customerField}>
        <label htmlFor={customerInputId}>Número de cuenta de Google Ads{googleCustomerIdRequired ? "" : " (opcional)"}</label>
        <input id={customerInputId} className={styles.customerInput} type="text" inputMode="numeric" autoComplete="off"
          placeholder="123-456-7890" value={googleCustomerId} required={googleCustomerIdRequired}
          disabled={startReconnect.isPending || waitingForConsent}
          aria-describedby={`${customerInputId}-help`}
          onChange={(event) => setGoogleCustomerId(event.target.value)} />
        <span id={`${customerInputId}-help`}>Lo encontrarás arriba en Google Ads. Son 10 dígitos; no es una clave ni tu correo.</span>
      </div> : null}
      {needsSetup && onSetup ? <>
        <p className={styles.setupDescription}>{provider === "meta"
          ? "Necesitas el App ID y el App Secret de tu aplicación Meta. Te mostramos dónde encontrarlos y qué guardar; después autorizas tu cuenta."
          : guidedSetup
            ? "Prepara Google Ads con la conexión de Composio que usas en Safent. Después inicia sesión y elige tu cuenta publicitaria."
            : "Primero configura la aplicación de Google. Después podrás iniciar sesión y autorizar tu cuenta publicitaria."}</p>
        <button type="button" className={styles.button} onClick={onSetup}>
          Configurar {providerName} paso a paso
        </button>
      </> : <button
        type="button"
        className={styles.button}
        onClick={() => void handleConnect()}
        disabled={!configured || !ownerId || !businessId || startReconnect.isPending || waitingForConsent}
      >
        Conectar
      </button>}
      {configurationPending ? <span className={styles.status} role="status">Comprobando la conexión de {providerName}…</span> : null}
      {configurationError ? <span className={styles.status}>No se pudo comprobar la configuración. Pulsa «Reintentar» arriba.</span> : null}
      {configured && guidedSetup && onSetup ? <button type="button" className={styles.setupLink} onClick={onSetup}>Revisar configuración de {providerName}</button> : null}
      {startReconnect.isPending ? <span className={styles.status} role="status">Preparando la conexión…</span> : null}
      {startError ? <span className={styles.status} role="alert">{startError}</span> : null}
      {storageUnavailable ? <span className={styles.status} role="alert">Esta ventana no puede recordar el intento. Mantén Safent abierto hasta comprobar el resultado.</span> : null}
      {needsSetup && !onSetup ? (
        <span className={styles.status} role="status">
          {provider === "meta" ? "Configura el App ID y el App Secret de tu aplicación Meta antes de autorizar la cuenta." : "Configura la conexión de Google antes de autorizar la cuenta."}
        </span>
      ) : null}
      {waitingForConsent && authorizeUrl && configured ? <>
        {asksForGoogleAccount ? (
          <span className={styles.status}>
            En la siguiente pantalla, «Customer ID» es el número de tu cuenta de Google Ads: 10 dígitos.
            Lo encontrarás arriba en Google Ads. No es una clave ni tu correo electrónico.
            Después iniciarás sesión y autorizarás el acceso.
          </span>
        ) : null}
        <span className={styles.status}>Si no se abre el navegador, continúa con este enlace. Después vuelve a Safent para comprobar el resultado.</span>
        <a className={styles.actionLink} href={authorizeUrl} target="_blank" rel="noopener noreferrer" onClick={(event) => {
          const openNative = nativeOAuthOpener();
          if (!openNative) return;
          event.preventDefault();
          const startedContext = context.current;
          setStartError(null);
          void openNative(authorizeUrl).catch(() => {
            if (context.current === startedContext) setStartError("No pudimos abrir el navegador. Vuelve a pulsar el enlace para reintentarlo.");
          });
        }}>
          Continuar en {provider === "google" ? "Google" : "Meta"}
        </a>
      </> : null}
      {sessionId && status.isError ? <>
        <span className={styles.status} role="alert">{pollingFailureMessage(status.error)}</span>
        <button type="button" className={styles.button} disabled={status.isFetching} onClick={() => void refetch({ cancelRefetch: false })}>
          {status.isFetching ? "Comprobando…" : "Reintentar comprobación"}
        </button>
      </> : null}
      {sessionId && !status.isError ? (
        <span className={styles.status} role={state === "error" ? "alert" : "status"}>
          {state === "waiting" || !state
            ? "Esperando a la plataforma…"
            : state === "ok"
              ? "Conexión completada."
              : (status.data?.message?.trim() || DEFAULT_ERROR_MESSAGE)}
        </span>
      ) : null}
      {provider === "google" && state === "error" && status.data?.error_code === GOOGLE_CLOUD_ACCESS_ERROR_CODE ? (
        <a
          className={styles.actionLink}
          href={GOOGLE_CLOUD_ADS_API_CONSOLE_URL}
          target="_blank"
          rel="noopener noreferrer"
        >
          Revisar acceso en Google Cloud Console
        </a>
      ) : null}
    </div>
  );
}
