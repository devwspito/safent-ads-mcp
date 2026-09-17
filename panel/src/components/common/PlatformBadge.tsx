import type { Platform } from "@/api/schemas";
import { platformShortLabel } from "@/utils/platform";
import styles from "./PlatformBadge.module.css";

interface PlatformBadgeProps {
  platform: Platform;
}

/** Ficha de plataforma — glifo de color + etiqueta, nunca el color solo como canal (WCAG 1.4.1). */
export function PlatformBadge({ platform }: PlatformBadgeProps) {
  return (
    <span className={styles.badge}>
      <span className={`${styles.glyph} ${styles[platform]}`} aria-hidden="true" />
      {platformShortLabel(platform)}
    </span>
  );
}
