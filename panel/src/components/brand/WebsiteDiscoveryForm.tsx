import { useState, type FormEvent } from "react";
import { ApiRequestError } from "@/api/client";
import { useDiscoverBrand } from "@/api/queries/brand";
import { isSafeUrl } from "@/utils/url";
import styles from "./WebsiteDiscoveryForm.module.css";

interface WebsiteDiscoveryFormProps {
  businessId: string;
  manualUploadAnchorId: string;
}

const DEFAULT_ERROR_MESSAGE = "No se pudo rastrear el sitio.";

/**
 * Rastreo opcional de identidad de marca desde el sitio web del negocio (rest-api.md §Marca).
 * `422 DISCOVERY_UNREACHABLE` no es un fallo de nuestro lado: se explica el motivo del servidor
 * y se ofrece el camino manual, nunca un mensaje genérico que empuje a reintentar sin salida.
 */
export function WebsiteDiscoveryForm({ businessId, manualUploadAnchorId }: WebsiteDiscoveryFormProps) {
  const [url, setUrl] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [succeeded, setSucceeded] = useState(false);
  const discoverBrand = useDiscoverBrand(businessId);

  const trimmedUrl = url.trim();
  const urlLooksValid = trimmedUrl.length === 0 || isSafeUrl(trimmedUrl);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!trimmedUrl || !isSafeUrl(trimmedUrl)) return;
    setErrorMessage(null);
    setUnreachable(false);
    setSucceeded(false);
    try {
      await discoverBrand.mutateAsync(trimmedUrl);
      setSucceeded(true);
    } catch (error) {
      if (error instanceof ApiRequestError) {
        setErrorMessage(error.message);
        setUnreachable(error.code === "DISCOVERY_UNREACHABLE");
        return;
      }
      setErrorMessage(DEFAULT_ERROR_MESSAGE);
    }
  }

  return (
    <form className={styles.form} onSubmit={(event) => void handleSubmit(event)}>
      <label className={styles.label} htmlFor="brand-website-url">
        Sitio web del negocio (opcional)
      </label>
      <div className={styles.row}>
        <input
          id="brand-website-url"
          className={styles.input}
          type="url"
          inputMode="url"
          placeholder="https://negocio-ejemplo.es"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          aria-invalid={!urlLooksValid}
          aria-describedby={!urlLooksValid ? "brand-website-url-error" : undefined}
        />
        <button type="submit" className={styles.button} disabled={!trimmedUrl || !urlLooksValid || discoverBrand.isPending}>
          {discoverBrand.isPending ? "Rastreando…" : "Rastrear"}
        </button>
      </div>
      {!urlLooksValid ? (
        <span id="brand-website-url-error" className={styles.error} role="alert">
          Escribe una URL http:// o https:// válida.
        </span>
      ) : null}

      {discoverBrand.isPending ? (
        <span className={styles.status} role="status">
          Rastreando el sitio…
        </span>
      ) : null}

      {succeeded ? (
        <span className={styles.status} role="status">
          Rastreo completado. Revisa los candidatos más abajo.
        </span>
      ) : null}

      {errorMessage ? (
        <div className={styles.errorBlock} role="alert">
          <p className={styles.error}>{errorMessage}</p>
          {unreachable ? (
            <a className={styles.manualLink} href={`#${manualUploadAnchorId}`}>
              Subir logotipos y tipografía a mano
            </a>
          ) : null}
        </div>
      ) : null}
    </form>
  );
}
