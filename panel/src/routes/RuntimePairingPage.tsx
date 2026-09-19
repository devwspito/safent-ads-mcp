import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { z } from "zod";
import { apiClient } from "@/api/client";
import { useMe } from "@/api/queries/auth";
import styles from "./ConsentimientoPage.module.css";

const requestSchema = z.object({
  challenge: z.string().regex(/^[a-f0-9]{64}$/),
  public_key: z.string().min(300).max(600),
  runtime: z.enum(["codex", "claude"]),
  label: z.string().min(1).max(80),
});

export function RuntimePairingPage() {
  const [request] = useState(() => requestSchema.safeParse(Object.fromEntries(new URLSearchParams(window.location.search))));
  const session = useMe();
  const [business, setBusiness] = useState("");
  const [fingerprint, setFingerprint] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [approved, setApproved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const businessId = business || session.data?.businesses[0]?.business_id || "";

  useEffect(() => {
    if (!request.success) return;
    let current = true;
    void crypto.subtle.digest("SHA-256", new TextEncoder().encode(request.data.challenge + request.data.public_key)).then(hash => {
      if (current) setFingerprint(Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("").slice(0, 12).toUpperCase());
    }).catch(() => { if (current) setError("No se pudo verificar el código de este equipo."); });
    return () => { current = false; };
  }, [request]);

  async function authorize() {
    if (!request.success || !confirmed || !fingerprint || !businessId || busy) return;
    setBusy(true); setError(null);
    try {
      await apiClient.post("/runtime/pair", z.object({ approved: z.literal(true), connection_id: z.string().uuid() }), request.data, { business_id: businessId });
      setApproved(true);
    } catch { setError("No se pudo autorizar. Si el enlace caducó, vuelve a ejecutar el instalador."); }
    finally { setBusy(false); }
  }

  return <main className={styles.wrap}><section className={styles.card} aria-label="Vincular runtime">
    <h1 className={styles.title}>Vincular este equipo</h1>
    {!request.success ? <p role="alert">Enlace de instalación inválido. Inicia el instalador desde tu equipo.</p>
      : approved ? <><p role="status">Autorizado. Vuelve a la terminal: el instalador verificará la conexión y terminará la configuración.</p><p className={styles.body}>Puedes revocar este acceso desde el detalle de una propuesta. La autorización no certifica que el proceso esté en ejecución.</p><Link to="/propuestas">Ver propuestas</Link></>
        : <>
          <dl className={styles.details}>
            <dt className={styles.detailLabel}>Equipo declarado por el instalador</dt><dd className={styles.detailValue}>{request.data.label}</dd>
            <dt className={styles.detailLabel}>Runtime</dt><dd className={styles.detailValue}>{request.data.runtime === "codex" ? "Codex" : "Claude Code"}</dd>
            <dt className={styles.detailLabel}>Código: debe coincidir con tu terminal</dt><dd className={styles.detailValue}>{fingerprint || "Verificando…"}</dd>
          </dl>
          <p className={styles.body}>Sólo preparar borradores de tu negocio, recibir encargos y devolver resultados. Acceso revocable durante 30 días. No permite publicar anuncios, gastar en publicidad ni enviar WhatsApp.</p>
          <p className={styles.body}>No autorices enlaces que te haya enviado otra persona. El nombre del equipo no demuestra su identidad: comprueba el código.</p>
          {!session.data ? <><p>Inicia sesión y vuelve a esta pestaña para autorizar.</p><Link to="/login" target="_blank" rel="noopener noreferrer">Iniciar sesión en otra pestaña</Link><button className={styles.secondaryButton} onClick={() => void session.refetch()} disabled={session.isFetching}>Comprobar sesión</button></>
            : <>
              <label>Negocio <select value={businessId} onChange={event => setBusiness(event.target.value)}>{session.data.businesses.map(item => <option key={item.business_id} value={item.business_id}>{item.name}</option>)}</select></label>
              <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> He iniciado esta instalación y el código coincide. Entiendo que los encargos consumen cuota de mi runtime cuando el conector está en ejecución.</label>
              <button className={styles.primaryButton} disabled={!confirmed || !fingerprint || !businessId || busy} onClick={() => void authorize()}>{busy ? "Autorizando…" : "Autorizar este equipo"}</button>
            </>}
          <Link to="/propuestas">Cancelar sin autorizar</Link>
        </>}
    {error ? <p role="alert" className={styles.formError}>{error}</p> : null}
  </section></main>;
}
