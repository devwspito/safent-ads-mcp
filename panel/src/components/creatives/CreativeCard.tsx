import type { CreativeAsset } from "@/api/schemas/creatives";
import { formatMoney } from "@/utils/money";
import styles from "./CreativeCard.module.css";

const SIGNAL_LABELS: Record<string, string> = {
  FATIGUE: "Fatiga",
  WINNER: "Ganadora",
  LOSER: "Perdedora",
  LEARNING: "En aprendizaje",
};

interface CreativeCardProps {
  asset: CreativeAsset;
  pendingActions?: {
    onPublish: () => void;
    onReject: () => void;
    onRegenerate: () => void;
  };
}

/**
 * Pieza de creatividad: miniatura en el formato real, retención, trazabilidad
 * señal → pieza → resultado — panel-interaction-spec.md §3.5.
 */
export function CreativeCard({ asset, pendingActions }: CreativeCardProps) {
  return (
    <div className={styles.card}>
      <div className={styles.previewWrap}>
        <img className={styles.preview} src={asset.preview_url} alt={`Vista previa de ${asset.label} en formato ${asset.format}`} width={160} height={160} loading="lazy" />
      </div>
      <div className={styles.body}>
        <span className={styles.label}>{asset.label}</span>

        <div className={styles.stats}>
          <span>{formatMoney(asset.spend)} gastados</span>
          {asset.hook_rate_pct !== null ? <span>Hook {asset.hook_rate_pct} %</span> : null}
          {asset.hold_rate_pct !== null ? <span>Hold {asset.hold_rate_pct} %</span> : null}
          {asset.frequency !== null ? <span>Frecuencia {asset.frequency}</span> : null}
          <span>{asset.days_in_rotation} d en rotación</span>
        </div>

        <div className={styles.chips}>
          <span className={styles.chip}>{SIGNAL_LABELS[asset.signal] ?? asset.signal}</span>
          {asset.signal_id ? <span className={styles.chip}>Señal {asset.signal_id}</span> : null}
          <span className={styles.chip}>{asset.ads_running_on.length} anuncio(s)</span>
          {asset.policy_verdict === "PASS" ? <span className={styles.chip}>Normas OK</span> : null}
        </div>

        {asset.policy_verdict === "FAIL" ? (
          <span className={styles.policyFail}>{asset.policy_findings[0] ?? "No cumple las normas de la plataforma."}</span>
        ) : null}

        {pendingActions ? (
          <div className={styles.actions}>
            <button
              type="button"
              className={`${styles.actionButton} ${styles.publishButton}`}
              onClick={pendingActions.onPublish}
              disabled={asset.policy_verdict !== "PASS"}
              title={asset.policy_verdict !== "PASS" ? "La revisión de normas debe estar aprobada antes de preparar la publicación." : undefined}
            >
              Preparar publicación
            </button>
            <button type="button" className={styles.actionButton} onClick={pendingActions.onReject}>
              Rechazar
            </button>
            <button type="button" className={styles.actionButton} onClick={pendingActions.onRegenerate}>
              Regenerar
            </button>
            <a className={styles.actionButton} href={asset.preview_url} download={`${asset.asset_id}.svg`}>
              Descargar
            </a>
          </div>
        ) : null}
      </div>
    </div>
  );
}
