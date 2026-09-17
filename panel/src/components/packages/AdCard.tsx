import { useEffect, useRef, useState } from "react";
import { useCreativeJob, useRegenerateCreative } from "@/api/queries/creatives";
import { usePackageCreativeCandidates, usePatchPackageAdCreative } from "@/api/queries/packages";
import type { PackageAdPreview } from "@/api/schemas/packages";
import { isSafeUrl } from "@/utils/url";
import styles from "./AdCard.module.css";

interface AdCardProps {
  ad: PackageAdPreview;
  businessId: string;
  packageId: string;
  /** Huella vigente del paquete — obligatoria en el `PATCH` (contracts/api.md §6, INV-8). */
  packageHash: string;
}

type PendingAction = "candidate" | "regenerate" | null;

/**
 * Tarjeta de anuncio — `contracts/api.md` §7 punto 1: imagen ≥240 px, titular en negrita,
 * texto, descripción, píldora del botón, destino corto. `Cambiar imagen` abre un selector
 * en el sitio (nunca modal, design.md decisión 4); `Regenerar` encadena
 * `POST /creatives/{id}/regenerate` → `GET /creative-jobs/{id}` → `PATCH` del paquete (§6),
 * sin un segundo camino para lo mismo.
 */
export function AdCard({ ad, businessId, packageId, packageHash }: AdCardProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [regenerateJobId, setRegenerateJobId] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const handledJobId = useRef<string | null>(null);

  const candidates = usePackageCreativeCandidates(businessId, packageId, pickerOpen ? ad.local_ref : null);
  const patchMutation = usePatchPackageAdCreative(businessId, packageId);
  const regenerateMutation = useRegenerateCreative(businessId);
  const job = useCreativeJob(regenerateJobId);

  useEffect(() => {
    if (!job.data || job.data.state !== "READY" || handledJobId.current === job.data.job_id) return;
    handledJobId.current = job.data.job_id;
    const winner = job.data.assets.find((asset) => asset.policy_verdict === "PASS") ?? job.data.assets[0];
    if (!winner) {
      setRegenerateJobId(null);
      setPendingAction(null);
      return;
    }
    patchMutation.mutate(
      { adLocalRef: ad.local_ref, packageHash, creativeAssetId: winner.asset_id },
      {
        onError: () => setActionError("La imagen se regeneró, pero no se pudo aplicar al anuncio."),
        onSettled: () => {
          setRegenerateJobId(null);
          setPendingAction(null);
        },
      },
    );
  }, [job.data, ad.local_ref, packageHash, patchMutation]);

  function selectCandidate(assetId: string) {
    setActionError(null);
    setPendingAction("candidate");
    patchMutation.mutate(
      { adLocalRef: ad.local_ref, packageHash, creativeAssetId: assetId },
      {
        onError: () => setActionError("No se pudo cambiar la imagen."),
        onSuccess: () => setPickerOpen(false),
        onSettled: () => setPendingAction(null),
      },
    );
  }

  function regenerate() {
    if (!ad.image) return;
    setActionError(null);
    setPendingAction("regenerate");
    regenerateMutation.mutate(
      { assetId: ad.image.asset_id, reason: "Regenerar imagen del paquete" },
      {
        onSuccess: (result) => setRegenerateJobId(result.job_id),
        onError: () => {
          setActionError("No se pudo pedir una imagen nueva.");
          setPendingAction(null);
        },
      },
    );
  }

  // Sólo se excluye la PRIMERA "Titular" (la que ya se muestra como titular en negrita) — un
  // anuncio de búsqueda responsivo trae varias ("Titular" se repite); las demás siguen como
  // texto normal en vez de desaparecer.
  const headlineIndex = ad.texts.findIndex((text) => text.label === "Titular");
  const headline = headlineIndex >= 0 ? ad.texts[headlineIndex]!.value : ad.name;
  const restTexts = ad.texts.filter((_, index) => index !== headlineIndex);
  const busy = pendingAction !== null;

  return (
    <figure className={styles.card}>
      {ad.image ? (
        <img className={styles.image} src={ad.image.preview_url} alt={ad.image.alt} width={ad.image.width} height={ad.image.height} loading="lazy" />
      ) : (
        <div className={styles.textOnly}>Anuncio de solo texto</div>
      )}

      <figcaption className={styles.body}>
        <p className={styles.headline}>{headline}</p>
        {restTexts.map((text, index) => (
          <p key={`${text.label}-${index}`} className={styles.text}>
            {text.value}
          </p>
        ))}
        {ad.cta_label ? <span className={styles.ctaPill}>{ad.cta_label}</span> : null}
        {isSafeUrl(ad.landing.url) ? (
          <a className={styles.landing} href={ad.landing.url} target="_blank" rel="noopener noreferrer">
            {ad.landing.display}
          </a>
        ) : (
          <span className={styles.landing}>{ad.landing.display}</span>
        )}
      </figcaption>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.actionButton}
          disabled={!ad.actions.can_replace_image || busy}
          aria-expanded={pickerOpen}
          onClick={() => setPickerOpen((value) => !value)}
        >
          Cambiar imagen
        </button>
        <button
          type="button"
          className={styles.actionButton}
          disabled={!ad.actions.can_regenerate || !ad.image || busy}
          aria-busy={pendingAction === "regenerate"}
          onClick={regenerate}
        >
          {pendingAction === "regenerate" ? "Regenerando…" : "Regenerar"}
        </button>
      </div>

      <div role="status" aria-live="polite" className="visually-hidden">
        {pendingAction === "candidate" ? "Cambiando imagen…" : pendingAction === "regenerate" ? "Regenerando imagen…" : ""}
      </div>

      {actionError ? (
        <p role="alert" className={styles.actionError}>
          {actionError}
        </p>
      ) : null}

      {pickerOpen ? (
        <div className={styles.picker}>
          <p className={styles.pickerLabel}>Elige una imagen ya lista</p>
          {candidates.isLoading ? <p className={styles.pickerHint}>Buscando imágenes disponibles…</p> : null}
          {candidates.isError ? (
            <p role="alert" className={styles.pickerHint}>
              No se pudieron cargar las imágenes.
            </p>
          ) : null}
          {candidates.data && candidates.data.items.length === 0 ? <p className={styles.pickerHint}>No hay otra imagen lista todavía.</p> : null}
          <ul className={styles.pickerList}>
            {candidates.data?.items.map((candidate) => (
              <li key={candidate.asset_id}>
                <button type="button" className={styles.pickerItem} disabled={busy} onClick={() => selectCandidate(candidate.asset_id)}>
                  <img src={candidate.preview_url} alt="" width={80} height={42} />
                  <span>Usar esta imagen</span>
                </button>
              </li>
            ))}
          </ul>
          <button type="button" className={styles.pickerCancel} onClick={() => setPickerOpen(false)}>
            Cancelar
          </button>
        </div>
      ) : null}
    </figure>
  );
}
