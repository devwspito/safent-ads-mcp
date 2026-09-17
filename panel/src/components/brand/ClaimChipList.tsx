import { useState, type KeyboardEvent } from "react";
import {
  CLAIM_MAX_LENGTH,
  claimLengthError,
  hasCaseInsensitiveMatch,
  normalizeClaim,
} from "@/utils/brandClaims";
import styles from "./ClaimChipList.module.css";

interface ClaimChipListProps {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  addPlaceholder: string;
  inputId: string;
  /** Reclamos de solo lectura que se pintan primero, sin botón de quitar (el suelo de
   * seguridad de `forbidden_claims`, `is_floor: true`). */
  floorItems?: string[];
}

/**
 * Lista de reclamos con alta/baja (permitidos o prohibidos, `PUT /brand/claims`):
 * valida en el momento (2–80 caracteres tras recortar espacios, sin duplicados dentro de
 * la misma lista) — el mismo criterio que aplica el servidor, para que el error no dependa
 * de un viaje de ida y vuelta.
 */
export function ClaimChipList({ label, items, onChange, addPlaceholder, inputId, floorItems = [] }: ClaimChipListProps) {
  const [draft, setDraft] = useState("");
  const [inlineError, setInlineError] = useState<string | null>(null);

  function handleAdd() {
    const claim = normalizeClaim(draft);
    if (!claim) return;
    const error = validateNewClaim(claim, items, floorItems);
    if (error) {
      setInlineError(error);
      return;
    }
    onChange([...items, claim]);
    setDraft("");
    setInlineError(null);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    handleAdd();
  }

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={inputId}>
        {label}
      </label>
      <ul className={styles.chipRow}>
        {floorItems.map((claim) => (
          <li key={`floor-${claim}`} className={styles.floorChip} title="Suelo de seguridad: no editable">
            {claim}
          </li>
        ))}
        {items.map((claim) => (
          <li key={claim} className={styles.chip}>
            {claim}
            <button type="button" className={styles.chipRemove} onClick={() => onChange(items.filter((item) => item !== claim))} aria-label={`Quitar ${claim}`}>
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className={styles.row}>
        <input
          id={inputId}
          className={styles.input}
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setInlineError(null);
          }}
          onKeyDown={handleKeyDown}
          placeholder={addPlaceholder}
          maxLength={CLAIM_MAX_LENGTH}
          aria-invalid={inlineError !== null}
          aria-describedby={inlineError ? `${inputId}-error` : undefined}
        />
        <button type="button" className={styles.addButton} onClick={handleAdd}>
          Añadir
        </button>
      </div>
      {inlineError ? (
        <span id={`${inputId}-error`} className={styles.error} role="alert">
          {inlineError}
        </span>
      ) : null}
    </div>
  );
}

function validateNewClaim(claim: string, items: string[], floorItems: string[]): string | null {
  const lengthError = claimLengthError(claim);
  if (lengthError) return lengthError;
  if (hasCaseInsensitiveMatch(claim, items) || hasCaseInsensitiveMatch(claim, floorItems)) {
    return "Ya está en la lista.";
  }
  return null;
}
