import { useState } from "react";
import { useMe } from "@/api/queries/auth";
import { useCreatives, usePublishCreative, useRegenerateCreative, useRejectCreative } from "@/api/queries/creatives";
import type { CreativeAsset } from "@/api/schemas/creatives";
import { CreativeCard } from "@/components/creatives/CreativeCard";
import { CreativePublicationDialog } from "@/components/creatives/CreativePublicationDialog";
import { ErrorState } from "@/components/states/ErrorState";
import { PageLoading } from "@/components/states/PageLoading";
import { describeApiError } from "@/utils/apiError";
import { PageHeader } from "@/components/layout/PageHeader";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import styles from "./CreatividadesPage.module.css";

const SIGNAL_TABS: Array<{ value: string | undefined; label: string }> = [
  { value: undefined, label: "Todas" },
  { value: "FATIGUE", label: "Fatiga" },
  { value: "WINNER", label: "Ganadora" },
  { value: "LOSER", label: "Perdedora" },
  { value: "LEARNING", label: "En aprendizaje" },
];

type PendingDialog = { kind: "publish" | "reject" | "regenerate"; asset: CreativeAsset };

export function CreatividadesPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const [signalTab, setSignalTab] = useState<string | undefined>(undefined);
  const [dialog, setDialog] = useState<PendingDialog | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const galleryQuery = useCreatives({ business_id: businessId, signal: signalTab, pending_approval: false });
  const laneQuery = useCreatives({ business_id: businessId, pending_approval: true });
  const publish = usePublishCreative(businessId);
  const reject = useRejectCreative(businessId);
  const regenerate = useRegenerateCreative(businessId);

  const pending = laneQuery.data?.items ?? [];
  const gallery = galleryQuery.data?.items ?? [];

  async function handleConfirm(reason: string) {
    if (!dialog || dialog.asset.business_id !== businessId || laneQuery.isError || laneQuery.isFetching) throw new Error("Refresh required");
    if (dialog.kind === "reject") await reject.mutateAsync({ assetId: dialog.asset.asset_id, reason });
    if (dialog.kind === "regenerate") await regenerate.mutateAsync({ assetId: dialog.asset.asset_id, reason });
    setDialog(null);
  }

  return (
    <div>
      <PageHeader title="Creatividades" />
      {notice ? <p role="status" className={styles.notice}>{notice}</p> : null}
      {laneQuery.isLoading ? <PageLoading label="Cargando piezas por aprobar…" /> : null}
      {laneQuery.isError ? <div className={styles.laneStatus}><ErrorState message={describeApiError(laneQuery.error)} onRetry={() => void laneQuery.refetch()} /></div> : null}

      {pending.length > 0 ? (
        <fieldset className={styles.lane} disabled={laneQuery.isError || laneQuery.isFetching || publish.isPending || reject.isPending || regenerate.isPending}>
          <h2 className={styles.laneTitle}>Por aprobar ({pending.length})</h2>
          <div className={styles.laneGrid}>
            {pending.map((asset) => (
              <CreativeCard
                key={asset.asset_id}
                asset={asset}
                pendingActions={{
                  onPublish: () => setDialog({ kind: "publish", asset }),
                  onReject: () => setDialog({ kind: "reject", asset }),
                  onRegenerate: () => setDialog({ kind: "regenerate", asset }),
                }}
              />
            ))}
          </div>
        </fieldset>
      ) : null}

      <div className={styles.tabs} role="group" aria-label="Filtrar por señal">
        {SIGNAL_TABS.map((tab) => (
          <button
            key={tab.label}
            type="button"
            className={`${styles.tab} ${signalTab === tab.value ? styles.tabActive : ""}`}
            aria-pressed={signalTab === tab.value}
            onClick={() => setSignalTab(tab.value)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <QueryBoundary
        isLoading={galleryQuery.isLoading}
        isError={galleryQuery.isError}
        error={galleryQuery.error}
        onRetry={() => void galleryQuery.refetch()}
        data={galleryQuery.data}
        isEmpty={() => gallery.length === 0}
        emptyTitle="Sin creatividades con este filtro"
        emptyBody="Cuando el sistema genere o detecte piezas para esta señal, aparecerán aquí."
      >
        {() => (
          <div className={styles.gallery}>
            {gallery.map((asset) => (
              <CreativeCard key={asset.asset_id} asset={asset} />
            ))}
          </div>
        )}
      </QueryBoundary>

      {dialog?.kind === "publish" ? <CreativePublicationDialog asset={dialog.asset} businessId={businessId} onClose={() => setDialog(null)} onConfirm={async (input) => {
        if (laneQuery.isError || laneQuery.isFetching) throw new Error("Refresh required");
        await publish.mutateAsync({ assetId: dialog.asset.asset_id, ...input });
        setNotice("Propuesta de publicación creada. Revísala y apruébala en Propuestas; el anuncio todavía no se ha publicado.");
      }} /> : dialog ? <CreativeDialog dialog={dialog} onConfirm={handleConfirm} onClose={() => setDialog(null)} /> : null}
    </div>
  );
}

function CreativeDialog({ dialog, onConfirm, onClose }: { dialog: PendingDialog; onConfirm: (reason: string) => Promise<void>; onClose: () => void }) {
  if (dialog.kind === "reject") {
    return (
      <TypedConfirmDialog
        title="Rechazar pieza"
        description="Dinos por qué la rechazas para que el generador aprenda."
        confirmLabel="Rechazar"
        danger
        reasonRequired
        onConfirm={onConfirm}
        onClose={onClose}
      />
    );
  }
  return (
    <TypedConfirmDialog
      title="Regenerar pieza"
      description="Indica qué cambiar en la nueva versión."
      confirmLabel="Regenerar"
      reasonRequired
      onConfirm={onConfirm}
      onClose={onClose}
    />
  );
}
