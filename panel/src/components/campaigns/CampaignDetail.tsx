import { Link } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { useDecisionLog } from "@/api/queries/decisionLog";
import { useEntityChildren } from "@/api/queries/entities";
import { useProposals } from "@/api/queries/proposals";
import { isPackageFeedItem, type ProposalItem } from "@/api/schemas/proposals";
import type { EntityChildRow } from "@/api/schemas";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { formatMoney } from "@/utils/money";
import { plainEntityStatusLabel } from "@/utils/entityStatus";
import { describeProposalChange, proposalStateLabel } from "@/utils/proposals";
import { formatRelativeTime } from "@/utils/time";
import styles from "./CampaignDetail.module.css";

interface CampaignDetailProps {
  entityRef: string;
}

/**
 * Un nivel más bajo la fila (design.md §4.8): grupos de anuncios y sus anuncios, más el
 * historial de lo aplicado a esta campaña. Nunca cambia de pantalla.
 */
export function CampaignDetail({ entityRef }: CampaignDetailProps) {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const children = useEntityChildren(entityRef);
  const history = useDecisionLog({ business_id: businessId, entity_ref: entityRef });
  const proposals = useProposals({ business_id: businessId, lens: "urgency" });
  const proposalsOnCampaign = (proposals.data?.pages ?? [])
    .flatMap((page) => page.groups)
    .flatMap((group) => group.proposals)
    .filter((item): item is ProposalItem => !isPackageFeedItem(item) && item.entity_ref === entityRef);

  return (
    <div className={styles.wrap}>
      <section>
        <h4 className={styles.sectionTitle}>Propuestas sobre esta campaña</h4>
        {proposals.isLoading ? <p className={styles.empty}>Cargando…</p> : null}
        {!proposals.isLoading && proposalsOnCampaign.length === 0 ? <p className={styles.empty}>Sin propuestas para esta campaña.</p> : null}
        {proposalsOnCampaign.map((item) => (
          <p key={item.proposal_id} className={styles.historyItem}>
            {describeProposalChange(item)} · {proposalStateLabel(item.state)}
          </p>
        ))}
        {proposalsOnCampaign.length > 0 ? (
          <Link to="/propuestas" className={styles.viewLink}>
            Ver en Propuestas →
          </Link>
        ) : null}
      </section>

      <section>
        <h4 className={styles.sectionTitle}>Grupos y anuncios</h4>
        {children.isLoading ? <p className={styles.empty}>Cargando…</p> : null}
        {children.isError ? <p className={styles.empty} role="alert">No se pudo cargar.</p> : null}
        {children.data && children.data.items.length === 0 ? <p className={styles.empty}>Sin grupos de anuncios todavía.</p> : null}
        {children.data?.items.map((group) => <AdGroupSection key={group.entity_ref} group={group} />)}
      </section>

      <section>
        <h4 className={styles.sectionTitle}>Historial</h4>
        {history.isLoading ? <p className={styles.empty}>Cargando…</p> : null}
        {history.data && history.data.items.length === 0 ? <p className={styles.empty}>Nada aplicado todavía a esta campaña.</p> : null}
        {history.data?.items.map((entry) => (
          <p key={entry.seq} className={styles.historyItem}>
            {entry.summary} · {entry.actor_label} · {formatRelativeTime(entry.occurred_at)}
          </p>
        ))}
      </section>
    </div>
  );
}

function AdGroupSection({ group }: { group: EntityChildRow }) {
  const ads = useEntityChildren(group.has_children ? group.entity_ref : null);
  if (!group.has_children) return <ChildRow row={group} />;

  return (
    <div>
      <p className={styles.groupTitle}>{group.name}</p>
      {ads.data?.items.length === 0 ? (
        <p className={styles.empty}>Sin anuncios todavía.</p>
      ) : (
        ads.data?.items.map((ad) => <ChildRow key={ad.entity_ref} row={ad} />)
      )}
    </div>
  );
}

function ChildRow({ row }: { row: EntityChildRow }) {
  return (
    <div className={styles.childRow}>
      <span className={styles.childName}>{row.name}</span>
      <span>{plainEntityStatusLabel(row.status)}</span>
      <span>{row.spend_today ? formatMoney(row.spend_today) : "—"}</span>
      <span>{row.cost_per_lead ? `${formatMoney(row.cost_per_lead)}/lead` : "—"}</span>
    </div>
  );
}
