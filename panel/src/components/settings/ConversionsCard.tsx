import { useId, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { ApiRequestError } from "@/api/client";
import { useGenerateWebhookToken, useImportConversions } from "@/api/queries/economics";
import type { ConversionsImportResult } from "@/api/schemas/economics";
import { ActionConfirmationDialog } from "@/components/common/ActionConfirmationDialog";
import { useConfirmedMutation } from "@/hooks/useConfirmedMutation";
import styles from "./ConversionsCard.module.css";

interface ConversionsCardProps {
  businessId: string;
}

const DEFAULT_IMPORT_ERROR = "No se pudo importar el CSV.";

function buildWebhookUrl(): string {
  return `${window.location.origin}/api/v1/conversions/webhook`;
}

/** "Conversiones" (T220, contracts/rest-api.md §Conversiones): CSV exportado del CRM del
 * propietario, o un webhook en vivo con un token propio -- las dos vías que alimentan
 * `lead_attributions` sin asumir ningún CRM concreto (owner decision: "sin asunciones de CRM
 * vertical"). Mismo patrón de subida que `BrandAssetUploadForm` (arrastrar sobre un
 * `<input type=file>` normal, operable con teclado). */
export function ConversionsCard({ businessId }: ConversionsCardProps) {
  const importConversions = useImportConversions(businessId);
  const generateToken = useGenerateWebhookToken(businessId);

  const [dragging, setDragging] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<ConversionsImportResult | null>(null);
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const tokenConfirmation = useConfirmedMutation<Record<string, never>, { token: string }>({
    scopeKey: `webhook:${businessId}`,
    summarize: () => [`Negocio: ${businessId}`, "Reemplazar el token de conversiones anterior."],
    mutate: (variables) => generateToken.mutateAsync(variables),
    onSuccess: ({ token }) => {
      setIssuedToken(token);
      setCopied(false);
    },
  });

  const fileInputId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const webhookUrl = buildWebhookUrl();

  async function submitFile(file: File) {
    setImportError(null);
    setImportResult(null);
    try {
      setImportResult(await importConversions.mutateAsync(file));
    } catch (error) {
      setImportError(error instanceof ApiRequestError ? error.message : DEFAULT_IMPORT_ERROR);
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

  async function handleCopyToken() {
    if (!issuedToken) return;
    try {
      await navigator.clipboard.writeText(issuedToken);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className={styles.card} aria-labelledby="conversiones-heading">
      <h2 id="conversiones-heading" className={styles.heading}>
        Conversiones
      </h2>

      <div className={styles.subsection}>
        <p className={styles.hint}>
          Sube un CSV del CRM con las columnas <code>occurred_at</code>, <code>kind</code> (lead, whatsapp, call, business_conversion) y al
          menos una de <code>email</code>, <code>phone</code> o <code>external_ref</code>.
        </p>
        <div
          className={dragging ? `${styles.dropzone} ${styles.dropzoneActive}` : styles.dropzone}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
        >
          <p className={styles.dropHint}>Arrastra el CSV aquí, o</p>
          <label className={styles.fileButton} htmlFor={fileInputId}>
            Elegir archivo
          </label>
          <input
            id={fileInputId}
            ref={fileInputRef}
            className={styles.fileInput}
            type="file"
            accept=".csv,text/csv"
            onChange={handleFileInputChange}
            disabled={importConversions.isPending}
          />
        </div>

        {importConversions.isPending ? (
          <span className={styles.status} role="status">
            Importando…
          </span>
        ) : null}
        {importResult ? (
          <div className={styles.status} role="status">
            <p>
              {importResult.imported} importadas, {importResult.duplicates} duplicadas
              {importResult.rejected.length > 0 ? `, ${importResult.rejected.length} rechazadas` : ""}.
            </p>
            {importResult.rejected.length > 0 ? (
              <ul className={styles.rejectedList}>
                {importResult.rejected.map((row) => (
                  <li key={row.line}>
                    Línea {row.line}: {row.reason}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        {importError ? (
          <span className={styles.error} role="alert">
            {importError}
          </span>
        ) : null}
      </div>

      <div className={styles.subsection}>
        <p className={styles.hint}>O envía conversiones en vivo a esta URL, identificándote con un token propio.</p>
        <code className={styles.webhookUrl}>{webhookUrl}</code>

        <button type="button" className={styles.generate} onClick={() => tokenConfirmation.start({})} disabled={tokenConfirmation.isSubmitting}>
          Generar token
        </button>

        {issuedToken ? (
          <div className={styles.tokenReveal} role="status">
            <p className={styles.tokenWarning}>Guarda este token ahora: no se volverá a mostrar.</p>
            <div className={styles.tokenRow}>
              <code className={styles.token}>{issuedToken}</code>
              <button type="button" className={styles.copy} onClick={() => void handleCopyToken()}>
                {copied ? "Copiado" : "Copiar"}
              </button>
            </div>
          </div>
        ) : null}
      </div>

      {tokenConfirmation.isPromptOpen ? (
        <ActionConfirmationDialog
          title="Confirmar generación del token"
          description="Se generará un token de conversiones y se invalidará el anterior para este negocio."
          confirmLabel="Generar token"
          summary={tokenConfirmation.summary}
          canConfirm={tokenConfirmation.canConfirm}
          isSubmitting={tokenConfirmation.isSubmitting}
          errorMessage={tokenConfirmation.errorMessage}
          onConfirm={tokenConfirmation.confirm}
          onClose={tokenConfirmation.cancel}
        />
      ) : null}
    </section>
  );
}
