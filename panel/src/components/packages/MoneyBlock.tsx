import { formatCockpitMoney } from "@/utils/money";
import type { PackagePreview } from "@/api/schemas/packages";
import styles from "./MoneyBlock.module.css";

interface MoneyBlockProps {
  money: PackagePreview["money"];
}

/** `contracts/api.md` §7 punto 3: tres cifras grandes, texto de sobrante ya redactado por el servidor. */
export function MoneyBlock({ money }: MoneyBlockProps) {
  return (
    <section aria-labelledby="package-money-heading" className={styles.wrap}>
      <h3 id="package-money-heading" className={styles.heading}>
        Dinero
      </h3>
      <dl className={styles.figures}>
        <div className={styles.figure}>
          <dt className={styles.label}>Al día</dt>
          <dd className={styles.value}>{formatCockpitMoney(money.daily)}</dd>
        </div>
        <div className={styles.figure}>
          <dt className={styles.label}>Al mes</dt>
          <dd className={styles.value}>{formatCockpitMoney(money.monthly_equivalent)}</dd>
        </div>
        <div className={styles.figure}>
          <dt className={styles.label}>Tope total</dt>
          <dd className={styles.value}>{formatCockpitMoney(money.total_cap)}</dd>
        </div>
      </dl>
      <p className={styles.envelope}>{money.envelope.label}</p>
    </section>
  );
}
