import { useId, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { ApiRequestError } from "@/api/client";
import { useUploadBrandAsset } from "@/api/queries/brand";
import type { AssetKind } from "@/api/schemas/brand";
import { ASSET_KIND_LABELS } from "@/utils/brand";
import styles from "./BrandAssetUploadForm.module.css";

interface BrandAssetUploadFormProps {
  businessId: string;
  anchorId: string;
}

/** Mismo tope que documenta rest-api.md §Marca ("supera el tope de tamaño (10 MiB)") — chequeo
 * temprano en el cliente; el servidor sigue siendo quien decide de verdad (defensa en profundidad). */
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

const UPLOADABLE_KINDS: AssetKind[] = ["logo_vector", "logo_raster", "icon", "reference_photo", "font_file", "palette_definition"];

const DEFAULT_ERROR_MESSAGE = "No se pudo subir el archivo.";

/** Camino manual (rest-api.md §Marca: "El usuario puede subir manual o poner el enlace"): arrastrar
 * y soltar por encima de un `<input type=file>` normal, así que sigue siendo operable con teclado. */
export function BrandAssetUploadForm({ businessId, anchorId }: BrandAssetUploadFormProps) {
  const [kind, setKind] = useState<AssetKind>("logo_vector");
  const [dragging, setDragging] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [lastUploadedLabel, setLastUploadedLabel] = useState<string | null>(null);
  const fileInputId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadAsset = useUploadBrandAsset(businessId);

  async function submitFile(file: File) {
    setErrorMessage(null);
    setLastUploadedLabel(null);
    if (file.size > MAX_UPLOAD_BYTES) {
      setErrorMessage(`El archivo supera el tope de ${MAX_UPLOAD_BYTES / (1024 * 1024)} MiB.`);
      return;
    }
    try {
      await uploadAsset.mutateAsync({ kind, file });
      setLastUploadedLabel(file.name);
    } catch (error) {
      setErrorMessage(error instanceof ApiRequestError ? error.message : DEFAULT_ERROR_MESSAGE);
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) void submitFile(file);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files[0];
    if (file) void submitFile(file);
  }

  return (
    <section id={anchorId} tabIndex={-1} className={styles.section} aria-labelledby={`${anchorId}-heading`}>
      <h3 id={`${anchorId}-heading`} className={styles.heading}>
        Subir a mano
      </h3>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fileInputId}-kind`}>
          Tipo de archivo
        </label>
        <select id={`${fileInputId}-kind`} className={styles.select} value={kind} onChange={(event) => setKind(event.target.value as AssetKind)}>
          {UPLOADABLE_KINDS.map((k) => (
            <option key={k} value={k}>
              {ASSET_KIND_LABELS[k]}
            </option>
          ))}
        </select>
      </div>

      <div
        className={dragging ? `${styles.dropzone} ${styles.dropzoneActive}` : styles.dropzone}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <p className={styles.dropHint}>Arrastra el archivo aquí, o</p>
        <label className={styles.fileButton} htmlFor={fileInputId}>
          Elegir archivo
        </label>
        <input
          id={fileInputId}
          ref={fileInputRef}
          className={styles.fileInput}
          type="file"
          onChange={handleFileInputChange}
          disabled={uploadAsset.isPending}
        />
        <p className={styles.sizeCap}>Tamaño máximo: 10 MiB.</p>
      </div>

      {uploadAsset.isPending ? (
        <span className={styles.status} role="status">
          Subiendo…
        </span>
      ) : null}
      {lastUploadedLabel ? (
        <span className={styles.status} role="status">
          «{lastUploadedLabel}» añadido como candidato.
        </span>
      ) : null}
      {errorMessage ? (
        <span className={styles.error} role="alert">
          {errorMessage}
        </span>
      ) : null}
    </section>
  );
}
