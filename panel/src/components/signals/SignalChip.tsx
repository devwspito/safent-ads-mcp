import type { CSSProperties } from "react";
import type { CreativeSignalKind, SignalKind } from "@/api/schemas";
import { signalKindLabel, signalTokenPrefix, strengthTier } from "@/utils/signals";
import styles from "./SignalChip.module.css";

interface SignalChipProps {
  kind: SignalKind | CreativeSignalKind | "ANOMALY";
  strength: number;
}

/**
 * Ficha de señal `SUBIR 82` — panel-visual-spec.md §4. El número acompaña siempre al nivel;
 * el color nunca es el único canal (WCAG 1.4.1) — glifo de nivel + etiqueta + número.
 */
export function SignalChip({ kind, strength }: SignalChipProps) {
  const tier = strengthTier(strength);
  const prefix = signalTokenPrefix(kind);
  const style: CSSProperties =
    tier === "weak"
      ? {}
      : {
          background: `var(--${prefix}-fill)`,
          borderColor: `var(--${prefix}-line)`,
          color: `var(--${prefix}-text)`,
        };

  return (
    <span className={`${styles.chip} ${styles[tier]}`} style={style}>
      <span aria-hidden="true">{tierGlyph(tier)}</span>
      {signalKindLabel(kind)} {strength}
    </span>
  );
}

function tierGlyph(tier: "weak" | "medium" | "strong"): string {
  if (tier === "strong") return "▲";
  if (tier === "medium") return "●";
  return "·";
}
