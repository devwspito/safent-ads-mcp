import { useMemo, useRef, useState, type FormEvent } from "react";
import { usePortfolio } from "@/api/queries/portfolio";
import { useEntityChildren } from "@/api/queries/entities";
import { usePlatformAccounts } from "@/api/queries/connections";
import type { CreativeAsset, ProposePublicationInput } from "@/api/schemas/creatives";
import { Modal } from "@/components/common/Modal";
import { ErrorState } from "@/components/states/ErrorState";
import { describeApiError } from "@/utils/apiError";
import { platformLabel } from "@/utils/platform";
import styles from "./CreativePublicationDialog.module.css";

interface Props {
  asset: CreativeAsset;
  businessId: string;
  onClose: () => void;
  onConfirm: (input: ProposePublicationInput) => Promise<unknown>;
}

/** Select a real destination and copy; this creates a proposal, not an ad. */
export function CreativePublicationDialog({ asset, businessId, onClose, onConfirm }: Props) {
  const campaigns = usePortfolio(businessId, "7D");
  const accounts = usePlatformAccounts(businessId);
  const [campaign, setCampaign] = useState("");
  const groups = useEntityChildren(campaign || null);
  const [group, setGroup] = useState("");
  const [headline, setHeadline] = useState("");
  const [copy, setCopy] = useState("");
  const [cta, setCta] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitted = useRef(false);
  const choices = groups.data?.parent_ref === campaign && !groups.isPlaceholderData
    ? groups.data.items.filter((item) => item.entity_ref.split(":")[1] === "ad_set") : [];
  const selectionFresh = !campaigns.isFetching && !campaigns.isError && !campaigns.isPlaceholderData
    && campaigns.data?.rows.some((row) => row.entity_ref === campaign)
    && !groups.isFetching && !groups.isError && choices.some((item) => item.entity_ref === group);
  const canSubmit = selectionFresh && asset.business_id === businessId && asset.policy_verdict === "PASS"
    && headline.trim() && copy.trim() && cta.trim() && typed === "PUBLICAR" && !busy;
  // Varias conexiones pueden compartir plataforma (HANDOFF-ADS02 §Selección ambigua): la
  // campaña siempre lleva la etiqueta de SU cuenta física, nunca solo el nombre de la
  // plataforma, para que dos "Google Ads" conectados nunca se confundan en el desplegable.
  const accountLabelById = useMemo(() => {
    const map = new Map<string, { label: string; identity?: string }>();
    if (accounts.isError || accounts.isPlaceholderData || accounts.isFetching) return map;
    const items = accounts.data?.items ?? [];
    for (const account of items) {
      const ambiguous = items.filter((item) => item.label === account.label).length > 1;
      const parts = account.platform_account_id.split(":");
      const connection = parts[1] === "account" && parts.length >= 5 ? parts[3] : undefined;
      let length = 8;
      if (connection) {
        const others = items.filter((item) => item.platform_account_id !== account.platform_account_id)
          .map((item) => item.platform_account_id.split(":")[3]);
        while (length < connection.length && others.some((other) => other !== connection && other?.slice(0, length) === connection.slice(0, length))) length++;
      }
      const identity = connection ? `${account.external_account_id} / ${connection.slice(0, length)}` : account.platform_account_id;
      map.set(account.platform_account_id, { label: account.label, identity: ambiguous ? identity : undefined });
    }
    return map;
  }, [accounts.data, accounts.isError, accounts.isPlaceholderData, accounts.isFetching]);
  function destinationLabel(row: { name: string; platform: "google" | "meta"; platform_account_id: string }) {
    const account = accountLabelById.get(row.platform_account_id);
    if (account?.identity) return `${account.identity} · ${row.name} · ${account.label}`;
    return account ? `${row.name} · ${account.label}` : `${row.name} · ${platformLabel(row.platform)} · ${row.platform_account_id}`;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit || submitted.current) return;
    submitted.current = true;
    setBusy(true);
    setError(null);
    try {
      await onConfirm({ ad_set_ref: group, ad_copy: { headline: headline.trim(), primary_text: copy.trim(), cta: cta.trim() }, typed_confirmation: typed });
      onClose();
    } catch (failure) {
      setError(describeApiError(failure));
    } finally {
      submitted.current = false;
      setBusy(false);
    }
  }

  return (
    <Modal label="Preparar publicación" onClose={onClose} busy={busy} width="min(560px, 100%)">
      <form className={styles.form} onSubmit={(event) => void submit(event)}>
        <h2>Preparar publicación</h2>
        <p>Selecciona el destino y revisa el texto de «{asset.label}». Se creará una propuesta: no se publicará ningún anuncio hasta aprobarla en Propuestas.</p>
        {campaigns.isError || groups.isError ? <ErrorState message={describeApiError(campaigns.error ?? groups.error)} onRetry={() => { void campaigns.refetch(); if (campaign) void groups.refetch(); }} /> : null}
        <fieldset disabled={busy} className={styles.fields}>
          <label>Campaña
            <select value={campaign} onChange={(event) => { setCampaign(event.target.value); setGroup(""); }} required disabled={campaigns.isLoading || campaigns.isError}>
              <option value="">{campaigns.isLoading ? "Cargando campañas…" : "Selecciona una campaña"}</option>
              {campaigns.data?.rows.filter((row) => row.entity_ref.split(":")[1] === "campaign").map((row) => <option key={row.entity_ref} value={row.entity_ref}>{destinationLabel(row)}</option>)}
            </select>
          </label>
          <label>Grupo de anuncios
            <select value={group} onChange={(event) => setGroup(event.target.value)} required disabled={!campaign || groups.isFetching || groups.isError}>
              <option value="">{groups.isFetching ? "Cargando grupos…" : "Selecciona un grupo"}</option>
              {choices.map((item) => <option key={item.entity_ref} value={item.entity_ref}>{item.name}</option>)}
            </select>
          </label>
          {campaign && !groups.isFetching && !groups.isError && choices.length === 0 ? <p role="status">Esta campaña no tiene grupos disponibles. Elige otra campaña; no se creará un destino automáticamente.</p> : null}
          <label>Título<input value={headline} onChange={(event) => setHeadline(event.target.value)} required maxLength={200} /></label>
          <label>Texto del anuncio<textarea rows={3} value={copy} onChange={(event) => setCopy(event.target.value)} required maxLength={5000} /></label>
          <label>Llamada a la acción<input value={cta} onChange={(event) => setCta(event.target.value)} required maxLength={100} placeholder="Por ejemplo: Más información" /></label>
          <label>Escribe PUBLICAR para confirmar la propuesta<input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" required /></label>
        </fieldset>
        {error ? <p role="alert" className={styles.error}>{error}</p> : null}
        <div className={styles.actions}>
          <button type="button" onClick={onClose} disabled={busy}>Cancelar</button>
          <button type="submit" className={styles.primary} disabled={!canSubmit}>{busy ? "Creando propuesta…" : "Crear propuesta"}</button>
        </div>
      </form>
    </Modal>
  );
}
