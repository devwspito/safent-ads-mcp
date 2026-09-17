import { Link } from "react-router-dom";
import styles from "./PackageLinkChip.module.css";

interface PackageLinkChipProps {
  packageId: string;
}

/**
 * Ficha discreta que enlaza al detalle del paquete al que pertenece una propuesta
 * (`contracts/api.md` §1). Misma gramática visual que las fichas neutras existentes
 * (`SignalChip` débil, `ClaimChipList.floorChip`): borde discontinuo, sin color nuevo.
 */
export function PackageLinkChip({ packageId }: PackageLinkChipProps) {
  return (
    <Link to={`/propuestas/paquete/${packageId}`} className={styles.chip} onClick={(event) => event.stopPropagation()}>
      Paquete
    </Link>
  );
}
