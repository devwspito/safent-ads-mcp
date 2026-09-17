import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiRequestError } from "@/api/client";
import type { MeResponse } from "@/api/schemas";
import { createInitialBusiness, readInitialBusinessSession } from "@/api/queries/initialBusiness";
import styles from "./BusinessOnboarding.module.css";

interface Props {
  ownerId: string;
  onReady: (session: MeResponse) => void;
}

export function BusinessOnboarding({ ownerId, onReady }: Props) {
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [currency, setCurrency] = useState("EUR");
  const [phase, setPhase] = useState<"editing" | "creating" | "checking" | "unconfirmed">("editing");
  const [message, setMessage] = useState("");
  const alive = useRef(true);
  const inFlight = useRef(false);
  const alert = useRef<HTMLParagraphElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    alive.current = true;
    nameInput.current?.focus();
    return () => { alive.current = false; };
  }, []);
  useEffect(() => { if (message) alert.current?.focus(); }, [message]);

  async function verify(allowAnotherAttempt: boolean) {
    if (!alive.current) return;
    setPhase("checking");
    try {
      const session = await readInitialBusinessSession();
      if (!alive.current) return;
      if (session.owner_id !== ownerId) {
        setPhase("unconfirmed");
        setMessage("La sesión ha cambiado. Recarga el panel antes de continuar.");
        return;
      }
      if (session.businesses.length) {
        onReady(session);
        return;
      }
      setPhase(allowAnotherAttempt ? "editing" : "unconfirmed");
      setMessage(allowAnotherAttempt
        ? "Hemos comprobado tu sesión: aún no hay un negocio. Revisa los datos y vuelve a intentarlo cuando quieras."
        : "Todavía no podemos confirmar el negocio en tu sesión. Comprueba de nuevo; no volveremos a crearlo automáticamente.");
    } catch {
      if (!alive.current) return;
      setPhase("unconfirmed");
      setMessage("No pudimos comprobar si el negocio se creó. Comprueba el estado antes de volver a enviarlo.");
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current || phase !== "editing") return;
    const input = { name: name.trim(), timezone: timezone.trim(), reference_currency: currency.trim() };
    const hasControlCharacter = Array.from(input.name).some(character => {
      const code = character.charCodeAt(0);
      return code <= 0x1f || code === 0x7f;
    });
    if (!input.name || hasControlCharacter || !/^[A-Z]{3}$/.test(input.reference_currency)) {
      setMessage("Indica un nombre y una moneda de referencia de tres letras mayúsculas.");
      return;
    }
    try { new Intl.DateTimeFormat("es", { timeZone: input.timezone }); }
    catch { setMessage("Indica una zona horaria válida, por ejemplo Europe/Madrid."); return; }
    inFlight.current = true;
    setMessage("");
    setPhase("creating");
    try {
      await createInitialBusiness(input);
      if (alive.current) await verify(false);
    } catch (error) {
      if (!alive.current) return;
      if (error instanceof ApiRequestError && [400, 401, 403, 422].includes(error.status)) {
        setPhase("editing");
        setMessage(error.code === "OWNER_CONFIGURATION_AMBIGUOUS"
          ? "No se puede crear el negocio con esta configuración de propietarios. Revisa la configuración de Safent."
          : "No se pudo crear el negocio. Revisa los datos y tu sesión antes de intentarlo de nuevo.");
      } else {
        await verify(!(error instanceof ApiRequestError && error.status === 409));
      }
    } finally { inFlight.current = false; }
  }

  async function check() {
    if (inFlight.current) return;
    inFlight.current = true;
    setMessage("");
    try { await verify(true); } finally { inFlight.current = false; }
  }

  const busy = phase === "creating" || phase === "checking";
  return <main className={styles.page} id="main-content">
    <section className={styles.panel} aria-labelledby="business-onboarding-title">
      <p className={styles.eyebrow}>Safent · Anuncios</p>
      <h1 id="business-onboarding-title">Tu negocio, primero</h1>
      <p className={styles.intro}>Dale un nombre al espacio donde organizarás tus anuncios. Después podrás conectar Google y Meta.</p>
      <form onSubmit={event => void submit(event)} aria-busy={busy}>
        <fieldset disabled={phase !== "editing"} className={styles.fields}>
          <legend className={styles.legend}>Datos del negocio</legend>
          <label htmlFor="initial-business-name">Nombre del negocio</label>
          <input ref={nameInput} id="initial-business-name" value={name} onChange={event => setName(event.target.value)} required maxLength={120} autoComplete="organization" placeholder="Nombre de tu marca o empresa" />
          <label htmlFor="initial-business-timezone">Zona horaria</label>
          <input id="initial-business-timezone" value={timezone} onChange={event => setTimezone(event.target.value)} required maxLength={64} spellCheck={false} aria-describedby="timezone-hint" />
          <p id="timezone-hint" className={styles.hint}>Para interpretar fechas y horarios. Por ejemplo, Europe/Madrid.</p>
          <label htmlFor="initial-business-currency">Moneda de referencia</label>
          <input id="initial-business-currency" value={currency} onChange={event => setCurrency(event.target.value.toUpperCase())} required maxLength={3} pattern="[A-Z]{3}" autoCapitalize="characters" spellCheck={false} aria-describedby="currency-hint" />
          <p id="currency-hint" className={styles.hint}>Código de tres letras, como EUR o USD. No cambia la moneda de tus cuentas publicitarias.</p>
        </fieldset>
        {message ? <p role="alert" tabIndex={-1} ref={alert} className={styles.message}>{message}</p> : null}
        <p className={styles.note}>Este paso no conecta cuentas, publica anuncios ni activa gastos.</p>
        <div className={styles.actions}>
          {phase === "unconfirmed"
            ? <button type="button" onClick={() => void check()}>Comprobar estado</button>
            : <button type="submit" disabled={busy}>{phase === "creating" ? "Creando negocio…" : phase === "checking" ? "Comprobando tu sesión…" : "Crear negocio y continuar"}</button>}
          {busy ? <span role="status">{phase === "checking" ? "Verificando el resultado…" : "Guardando los datos…"}</span> : null}
        </div>
      </form>
    </section>
  </main>;
}
