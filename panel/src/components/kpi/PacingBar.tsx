import styles from "./PacingBar.module.css";

interface PacingBarProps {
  index: number;
}

const SCALE_MAX = 150;

/**
 * Ritmo — panel-visual-spec.md §5: rango sano 95–105, rampa divergente (déficit azul,
 * exceso ámbar); eje seguro para deuteranopía, no depende de rojo/verde.
 */
export function PacingBar({ index }: PacingBarProps) {
  const clamped = Math.min(index, SCALE_MAX);
  const toPct = (value: number) => `${(value / SCALE_MAX) * 100}%`;
  const zone = index < 95 ? "markerLow" : index > 105 ? "markerHigh" : "markerHealthy";

  return (
    <div className={styles.wrap}>
      <div className={styles.track} role="img" aria-label={`Ritmo ${index} de 100, rango sano 95 a 105`}>
        <div className={styles.healthyZone} style={{ left: toPct(95), width: toPct(10) }} />
        <div className={styles.target} style={{ left: toPct(100) }} />
        <div className={`${styles.marker} ${styles[zone]}`} style={{ left: toPct(clamped) }} />
      </div>
      <span className={styles.readout}>{index} · sano 95–105</span>
    </div>
  );
}
