import { OfferingsEconomicsTable } from "./OfferingsEconomicsTable";
import styles from "./EconomiaSection.module.css";

interface EconomiaSectionProps {
  businessId: string;
}

/** Sección "Ofertas y economía" de Tu negocio (T131/T132, owner decision: "los números de
 * margen/IVA también desde la UI"): IVA, coste de entrega, coste comercial y devolución por
 * oferta, sin ruta propia, igual que `MarcaSection`. Conversiones (CSV/webhook) vive aparte,
 * como su propia sección de Tu negocio -- el orden lo fija la revisión de capturas de
 * design.md §6, no todo lo que toca dinero es la misma tarea para el dueño. */
export function EconomiaSection({ businessId }: EconomiaSectionProps) {
  return (
    <section className={styles.section} aria-labelledby="economia-heading">
      <h2 id="economia-heading" className={styles.sectionTitle}>
        Ofertas y economía
      </h2>
      <OfferingsEconomicsTable businessId={businessId} />
    </section>
  );
}
