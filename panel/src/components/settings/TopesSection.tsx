import { usePlatformAccounts } from "@/api/queries/connections";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { AccountHardCapsCard } from "./AccountHardCapsCard";
import styles from "./TopesSection.module.css";

/**
 * «Topes» en Ajustes (spec 008 §5, `quickstart.md` §C): el último control antes de que una
 * cuenta pueda gastar. Una instalación nueva deniega el 100 % de las escrituras y es a
 * propósito — aquí se ve cuánto puede gastar cada cuenta, de dónde sale ese tope y cuánto
 * margen deja el sobre que el operador declaró en `config/caps.yaml`.
 */
export function TopesSection({ businessId }: { businessId: string }) {
  const accountsQuery = usePlatformAccounts(businessId);

  return (
    <section className={styles.section}>
      <h2 className={styles.groupTitle}>Topes</h2>
      <p className={styles.intro}>Hasta dónde puede gastar cada cuenta. Sin tope, esa cuenta no puede gastar nada.</p>
      <QueryBoundary
        isLoading={accountsQuery.isLoading}
        isError={accountsQuery.isError}
        error={accountsQuery.error}
        onRetry={() => void accountsQuery.refetch()}
        data={accountsQuery.data}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="Ninguna cuenta conectada"
        emptyBody="Conecta una cuenta para fijarle un tope."
      >
        {(data) => (
          <ul className={styles.list}>
            {data.items.map((account) => (
              <li key={account.platform_account_id}>
                <AccountHardCapsCard account={account} />
              </li>
            ))}
          </ul>
        )}
      </QueryBoundary>
    </section>
  );
}
