import { useState } from "react";
import type { AssetKind } from "@/api/schemas/brand";
import { ASSET_KIND_LABELS, isImageAssetKind } from "@/utils/brand";
import styles from "./AssetPreviewThumb.module.css";

interface AssetPreviewThumbProps {
  previewUrl: string;
  alt: string;
  kind: AssetKind;
}

/**
 * `previewUrl` es `GET /brand/assets/{asset_id}/preview` (`brand.preview_url`, mismo campo que
 * `creative.preview_url`) — a diferencia de la clave de almacenamiento opaca (`storage_uri`/`url`),
 * esta sí sirve los bytes de verdad. Si la carga falla igualmente (activo aún sin vista previa
 * segura, red, etc.), la vista de reserva transmite el tipo de activo en vez de fingir una vista
 * previa que no hay (capacidad declarada, nunca fingida).
 */
export function AssetPreviewThumb({ previewUrl, alt, kind }: AssetPreviewThumbProps) {
  const [failed, setFailed] = useState(false);

  if (!isImageAssetKind(kind) || failed) {
    return (
      <div className={styles.fallback} role="img" aria-label={alt}>
        {ASSET_KIND_LABELS[kind]}
      </div>
    );
  }

  return (
    <img
      className={styles.thumb}
      src={previewUrl}
      alt={alt}
      width={96}
      height={96}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
