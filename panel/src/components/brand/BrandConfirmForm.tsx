import { useState, type FormEvent } from "react";
import styles from "./BrandConfirmForm.module.css";

export interface BrandConfirmFormFields {
  fontLicenceNote: string;
  secondaryFont: string;
  fontWeights: string;
  toneDescription: string;
  toneAdjectives: string;
  toneAvoid: string;
}

interface BrandConfirmFormProps {
  primaryFont: string;
  onPrimaryFontChange: (value: string) => void;
  isSubmitting: boolean;
  submitError: string | null;
  onSubmit: (fields: BrandConfirmFormFields) => void;
}

const EMPTY_FIELDS: BrandConfirmFormFields = {
  fontLicenceNote: "",
  secondaryFont: "",
  fontWeights: "",
  toneDescription: "",
  toneAdjectives: "",
  toneAvoid: "",
};

/**
 * `POST /brand/confirm`: único camino para que `is_confirmed` pase a verdadero (rest-api.md
 * §Marca) — reemplaza tipografía/paleta/tono enteros, así que el formulario pide todo lo que ese
 * cuerpo exige, no un subconjunto que luego el servidor rellenaría a medias.
 */
export function BrandConfirmForm({ primaryFont, onPrimaryFontChange, isSubmitting, submitError, onSubmit }: BrandConfirmFormProps) {
  const [fields, setFields] = useState<BrandConfirmFormFields>(EMPTY_FIELDS);

  function update<K extends keyof BrandConfirmFormFields>(key: K, value: BrandConfirmFormFields[K]) {
    setFields((prev) => ({ ...prev, [key]: value }));
  }

  const canSubmit = primaryFont.trim().length > 0 && fields.fontLicenceNote.trim().length > 0 && fields.toneDescription.trim().length > 0;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    onSubmit(fields);
  }

  return (
    <form className={styles.form} onSubmit={handleSubmit}>
      <h4 className={styles.heading}>Confirmar identidad de marca</h4>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="confirm-primary-font">
          Tipografía principal
        </label>
        <input
          id="confirm-primary-font"
          className={styles.input}
          value={primaryFont}
          onChange={(event) => onPrimaryFontChange(event.target.value)}
          placeholder="Elige un candidato arriba o escríbela"
          required
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="confirm-font-licence">
          Nota de licencia de la tipografía
        </label>
        <input
          id="confirm-font-licence"
          className={styles.input}
          value={fields.fontLicenceNote}
          onChange={(event) => update("fontLicenceNote", event.target.value)}
          placeholder="p. ej. Google Fonts, licencia SIL Open Font"
          required
        />
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="confirm-secondary-font">
            Tipografía secundaria (opcional)
          </label>
          <input
            id="confirm-secondary-font"
            className={styles.input}
            value={fields.secondaryFont}
            onChange={(event) => update("secondaryFont", event.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="confirm-font-weights">
            Pesos (separados por comas)
          </label>
          <input
            id="confirm-font-weights"
            className={styles.input}
            value={fields.fontWeights}
            onChange={(event) => update("fontWeights", event.target.value)}
            placeholder="regular, bold"
          />
        </div>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="confirm-tone-description">
          Tono de voz
        </label>
        <textarea
          id="confirm-tone-description"
          className={styles.textarea}
          value={fields.toneDescription}
          onChange={(event) => update("toneDescription", event.target.value)}
          rows={3}
          required
        />
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="confirm-tone-adjectives">
            Adjetivos (separados por comas)
          </label>
          <input
            id="confirm-tone-adjectives"
            className={styles.input}
            value={fields.toneAdjectives}
            onChange={(event) => update("toneAdjectives", event.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="confirm-tone-avoid">
            Evitar (separado por comas)
          </label>
          <input
            id="confirm-tone-avoid"
            className={styles.input}
            value={fields.toneAvoid}
            onChange={(event) => update("toneAvoid", event.target.value)}
          />
        </div>
      </div>

      <button type="submit" className={styles.submit} disabled={!canSubmit || isSubmitting}>
        {isSubmitting ? "Confirmando…" : "Confirmar"}
      </button>
      {submitError ? (
        <span className={styles.error} role="alert">
          {submitError}
        </span>
      ) : null}
    </form>
  );
}
