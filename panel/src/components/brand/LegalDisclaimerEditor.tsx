import { useState } from "react";
import type { Platform } from "@/api/schemas";
import type { UpdateBrandClaimsLegalDisclaimerInput } from "@/api/schemas/brand";
import { platformLabel } from "@/utils/platform";
import styles from "./LegalDisclaimerEditor.module.css";

interface LegalDisclaimerEditorProps {
  disclaimers: UpdateBrandClaimsLegalDisclaimerInput[];
  onChange: (disclaimers: UpdateBrandClaimsLegalDisclaimerInput[]) => void;
}

const ALL_PLATFORMS: Platform[] = ["google", "meta"];

function disclaimerScopeLabel(appliesTo: Platform[] | null): string {
  return appliesTo === null ? "Todas las plataformas" : appliesTo.map(platformLabel).join(", ");
}

/** Avisos legales de `PUT /brand/claims`: `applies_to` vacío = todas las plataformas
 * conectadas (`LegalDisclaimer.applies_to_platform`, dominio). */
export function LegalDisclaimerEditor({ disclaimers, onChange }: LegalDisclaimerEditorProps) {
  const [text, setText] = useState("");
  const [platforms, setPlatforms] = useState<Platform[]>([]);

  function togglePlatform(platform: Platform) {
    setPlatforms((prev) => (prev.includes(platform) ? prev.filter((p) => p !== platform) : [...prev, platform]));
  }

  function handleAdd() {
    const trimmed = text.trim();
    if (!trimmed) return;
    onChange([...disclaimers, { text: trimmed, applies_to: platforms.length > 0 ? platforms : null }]);
    setText("");
    setPlatforms([]);
  }

  function handleRemove(index: number) {
    onChange(disclaimers.filter((_, i) => i !== index));
  }

  return (
    <div className={styles.field}>
      <span className={styles.label}>Avisos legales</span>
      <ul className={styles.list}>
        {disclaimers.map((disclaimer, index) => (
          <li key={`${disclaimer.text}-${index}`} className={styles.item}>
            <span className={styles.text}>{disclaimer.text}</span>
            <span className={styles.scope}>{disclaimerScopeLabel(disclaimer.applies_to)}</span>
            <button type="button" className={styles.remove} onClick={() => handleRemove(index)} aria-label={`Quitar aviso: ${disclaimer.text}`}>
              ×
            </button>
          </li>
        ))}
      </ul>
      <textarea
        className={styles.textarea}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Texto del aviso legal"
        rows={2}
      />
      <div className={styles.platformRow}>
        {ALL_PLATFORMS.map((platform) => (
          <label key={platform} className={styles.checkboxLabel}>
            <input type="checkbox" checked={platforms.includes(platform)} onChange={() => togglePlatform(platform)} />
            {platformLabel(platform)}
          </label>
        ))}
      </div>
      <button type="button" className={styles.addButton} onClick={handleAdd} disabled={!text.trim()}>
        Añadir aviso
      </button>
    </div>
  );
}
