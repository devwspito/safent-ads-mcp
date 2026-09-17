import { useEffect, useState } from "react";
import { ApiRequestError } from "@/api/client";
import { useUpdateBrandClaims } from "@/api/queries/brand";
import type { BrandKit, UpdateBrandClaimsLegalDisclaimerInput } from "@/api/schemas/brand";
import { findConflictingClaims } from "@/utils/brandClaims";
import { ClaimChipList } from "./ClaimChipList";
import { LegalDisclaimerEditor } from "./LegalDisclaimerEditor";
import styles from "./BrandClaimsEditor.module.css";

interface BrandClaimsEditorProps {
  businessId: string;
  kit: BrandKit;
}

const DEFAULT_ERROR_MESSAGE = "No se pudo guardar la política de reclamos.";

function ownedForbiddenClaims(kit: BrandKit): string[] {
  return kit.forbidden_claims.filter((entry) => !entry.is_floor).map((entry) => entry.claim);
}

function floorClaims(kit: BrandKit): string[] {
  return kit.forbidden_claims.filter((entry) => entry.is_floor).map((entry) => entry.claim);
}

/**
 * Editor de `claims_allowlist`, `forbidden_claims` (añadidos del propio negocio, el suelo de
 * seguridad se pinta pero no se puede quitar) y `legal_disclaimers` (`PUT /brand/claims`,
 * rest-api.md §Marca) — el único bloque que `POST /brand/confirm` deliberadamente no gestiona.
 * Funciona igual sobre un kit confirmado que sobre una vista previa de borrador.
 */
export function BrandClaimsEditor({ businessId, kit }: BrandClaimsEditorProps) {
  const [allowlist, setAllowlist] = useState<string[]>(kit.claims_allowlist);
  const [forbidden, setForbidden] = useState<string[]>(ownedForbiddenClaims(kit));
  const [disclaimers, setDisclaimers] = useState<UpdateBrandClaimsLegalDisclaimerInput[]>(kit.legal_disclaimers);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const updateClaims = useUpdateBrandClaims(businessId);

  useEffect(() => {
    setAllowlist(kit.claims_allowlist);
    setForbidden(ownedForbiddenClaims(kit));
    setDisclaimers(kit.legal_disclaimers);
    setSaved(false);
  }, [kit]);

  const floor = floorClaims(kit);
  const conflicts = findConflictingClaims(allowlist, [...floor, ...forbidden]);
  const hasConflict = conflicts.length > 0;

  function handleSave() {
    setServerError(null);
    setSaved(false);
    updateClaims.mutate(
      { claims_allowlist: allowlist, forbidden_claims: forbidden, legal_disclaimers: disclaimers },
      {
        onSuccess: () => setSaved(true),
        onError: (error) => setServerError(error instanceof ApiRequestError ? error.message : DEFAULT_ERROR_MESSAGE),
      },
    );
  }

  return (
    <section className={styles.section} aria-labelledby="brand-claims-heading">
      <h3 id="brand-claims-heading" className={styles.heading}>
        Reclamos y avisos legales
      </h3>

      <ClaimChipList
        label="Reclamos permitidos"
        items={allowlist}
        onChange={setAllowlist}
        addPlaceholder="p. ej. Envío gratis"
        inputId="brand-claims-allow"
      />

      <ClaimChipList
        label="Reclamos prohibidos"
        items={forbidden}
        onChange={setForbidden}
        addPlaceholder="p. ej. Mejor del mercado"
        inputId="brand-claims-forbid"
        floorItems={floor}
      />

      <LegalDisclaimerEditor disclaimers={disclaimers} onChange={setDisclaimers} />

      {hasConflict ? (
        <p className={styles.error} role="alert">
          «{conflicts[0]}» está en reclamos permitidos y prohibidos a la vez. Quítalo de una de las dos listas.
        </p>
      ) : null}
      {serverError ? (
        <p className={styles.error} role="alert">
          {serverError}
        </p>
      ) : null}
      {saved ? (
        <p className={styles.status} role="status">
          Guardado.
        </p>
      ) : null}

      <button type="button" className={styles.saveButton} onClick={handleSave} disabled={hasConflict || updateClaims.isPending}>
        {updateClaims.isPending ? "Guardando…" : "Guardar"}
      </button>
    </section>
  );
}
