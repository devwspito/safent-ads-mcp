import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";
import { describeApiError } from "@/utils/apiError";
import styles from "./CloudflareConnectionCard.module.css";

const statusSchema = z.object({
  available: z.boolean(), configured: z.boolean(), base_url: z.string().nullable(),
  egress_ip: z.string().nullable(), updated_at: z.string().nullable(),
  resources: z.array(z.string()), read_only: z.boolean(),
});

export function StoreApiConnectionCard({ businessId }: { businessId: string }) {
  const query = useQuery({
    queryKey: ["integrations", "store-api", businessId], enabled: Boolean(businessId), retry: false,
    queryFn: () => apiClient.get("/integrations/store-api", statusSchema, { business_id: businessId }),
  });
  const [showForm, setShowForm] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setMessage(null);
    try {
      await apiClient.put("/integrations/store-api", statusSchema, { token: token.trim() }, { business_id: businessId });
      setShowForm(false);
      setMessage("Conexión comprobada y guardada. El MCP ya puede consultar el catálogo, los precios y el stock disponible.");
      await query.refetch();
    } catch (error) {
      setMessage(describeApiError(error));
    } finally { setToken(""); setBusy(false); }
  }

  return <div className={styles.card}>
    <div className={styles.header}><h4 className={styles.title}>Catálogo y stock</h4><span className={styles.status} role="status">{query.isPending ? "Comprobando…" : query.isError ? "Estado no disponible" : query.data?.configured ? "Conectado · solo lectura" : "No conectado"}</span></div>
    <p>Contexto permanente para el MCP: productos, precios y disponibilidad. Esta conexión es independiente de las campañas y no modifica inventario.</p>
    {query.isError ? <p role="alert" className={styles.error}>{describeApiError(query.error)} <button onClick={() => void query.refetch()}>Reintentar</button></p> : null}
    {query.data?.base_url ? <p>API: <code>{query.data.base_url}</code></p> : null}
    {query.data?.egress_ip ? <p>Si la conexión exige una IP autorizada, permite <code>{query.data.egress_ip}</code>.</p> : null}
    {query.data && !query.data.available ? <p>El operador todavía debe configurar la dirección de la API.</p> : null}
    {query.data?.available && !showForm ? <button className={styles.button} onClick={() => { setShowForm(true); setMessage(null); }}>{query.data.configured ? "Actualizar conexión" : "Conectar catálogo"}</button> : null}
    {showForm ? <form className={styles.form} onSubmit={event => void connect(event)}>
      <label className={styles.label} htmlFor="store-api-token">Token de acceso de la tienda</label>
      <input className={styles.input} id="store-api-token" type="password" autoComplete="off" spellCheck={false} value={token} maxLength={4096} onChange={event => setToken(event.target.value)} disabled={busy} />
      <p>Se comprueban los tres recursos antes de guardar. El token se guarda cifrado y nunca se muestra al agente ni se devuelve al navegador. No lo compartas por chat.</p>
      <div className={styles.actions}><button className={styles.button} type="submit" disabled={busy || !token.trim()}>{busy ? "Comprobando conexión…" : "Comprobar y guardar"}</button><button className={styles.button} type="button" disabled={busy} onClick={() => { setToken(""); setShowForm(false); }}>Cancelar</button></div>
    </form> : null}
    {message ? <p role="status">{message}</p> : null}
  </div>;
}
