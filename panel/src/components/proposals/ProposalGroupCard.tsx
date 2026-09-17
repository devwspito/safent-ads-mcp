import { useProposalDetail } from "@/api/queries/proposals";
import { isPackageFeedItem, type PackageFeedItem, type ProposalGroup, type ProposalItem } from "@/api/schemas/proposals";
import type { PlatformAccount } from "@/api/schemas/connections";
import { formatMoney, formatSignedMoney, monthlyEquivalent } from "@/utils/money";
import { describeProposalChange, expiresWithin24h, expiryCountdownLabel, feedItemId, proposalDailyMoney, proposalDisplayName } from "@/utils/proposals";
import { accountDisplayName, platformAccountDisplayName, platformShortLabel } from "@/utils/platform";
import { RowMenu } from "@/components/common/RowMenu";
import { ProposalDetailPanel } from "./ProposalDetailPanel";
import { PackageProposalRow } from "@/components/packages/PackageProposalRow";
import { PackageLinkChip } from "@/components/packages/PackageLinkChip";
import { campaignPlanReady } from "@/api/schemas/campaignCreation";
import styles from "./ProposalGroupCard.module.css";

export type PostponeOption = "tarde";

export interface ProposalGroupActions {
  onFocus: (id: string) => void;
  onToggleExpand: (id: string) => void;
  onApprove: (proposal: ProposalItem) => void;
  onReject: (proposal: ProposalItem) => void;
  onPostpone: (proposal: ProposalItem, option: PostponeOption) => void;
  onBatchApprove: (group: ProposalGroup) => void;
  onBatchReject: (group: ProposalGroup) => void;
  /** api.md §4 — «Aprobar y publicar» exige la huella vigente que vio el dueño, nunca la de la fila. */
  onApprovePackage: (item: PackageFeedItem, packageHash: string) => void;
  onRejectPackage: (item: PackageFeedItem) => void;
}

interface ProposalGroupCardProps {
  businessId: string;
  group: ProposalGroup;
  focusedProposalId: string | null;
  expandedProposalId: string | null;
  /** Motivo corto siempre a la vista bajo «Aprobar» cuando está deshabilitado — design.md §2.3. */
  writeDisabledReason: string | null;
  writeDisabledShortReason: string | null;
  /** `/platform-accounts` — única fuente fiable del nombre de cuenta mientras `account_label` viaje crudo. */
  accounts: PlatformAccount[];
  actions: ProposalGroupActions;
}

/** Tarjeta por causa — la gramática de fila de panel-interaction-spec.md §2.2 y §3. */
export function ProposalGroupCard({ businessId, group, focusedProposalId, expandedProposalId, writeDisabledReason, writeDisabledShortReason, accounts, actions }: ProposalGroupCardProps) {
  const hasPackage = group.proposals.some(isPackageFeedItem);
  const hasCreateCampaign = group.proposals.some((item) => !isPackageFeedItem(item) && item.action_kind === "create_campaign");
  // Un paquete nunca entra en un grupo por lotes — api.md §1, tasks.md T052.
  const canBatchApprove = group.batch_eligible && !hasCreateCampaign && !hasPackage;

  return (
    <div className={styles.card}>
      {canBatchApprove ? (
        <div className={styles.header}>
          <span className={styles.cause}>{group.cause}</span>
          <button type="button" className={styles.batchButton} disabled={Boolean(writeDisabledReason)} title={writeDisabledReason ?? undefined} onClick={() => actions.onBatchApprove(group)}>
            Aprobar las {group.count} iguales
          </button>
          <button type="button" className={styles.batchButtonSecondary} onClick={() => actions.onBatchReject(group)}>
            Rechazar todas
          </button>
        </div>
      ) : null}

      {group.proposals.map((item) =>
        isPackageFeedItem(item) ? (
          <PackageProposalRow
            key={item.package_id}
            businessId={businessId}
            item={item}
            isFocused={item.package_id === focusedProposalId}
            isExpanded={item.package_id === expandedProposalId}
            writeDisabledReason={writeDisabledReason}
            writeDisabledShortReason={writeDisabledShortReason}
            actions={{ onFocus: actions.onFocus, onToggleExpand: actions.onToggleExpand, onApprove: actions.onApprovePackage, onReject: actions.onRejectPackage }}
          />
        ) : (
          <ProposalRow
            key={item.proposal_id}
            proposal={item}
            isFocused={feedItemId(item) === focusedProposalId}
            isExpanded={feedItemId(item) === expandedProposalId}
            writeDisabledReason={writeDisabledReason}
            writeDisabledShortReason={writeDisabledShortReason}
            accounts={accounts}
            actions={actions}
          />
        ),
      )}
    </div>
  );
}

interface ProposalRowProps {
  proposal: ProposalItem;
  isFocused: boolean;
  isExpanded: boolean;
  writeDisabledReason: string | null;
  writeDisabledShortReason: string | null;
  accounts: PlatformAccount[];
  actions: ProposalGroupActions;
}

function ProposalRow({ proposal, isFocused, isExpanded, writeDisabledReason, writeDisabledShortReason, accounts, actions }: ProposalRowProps) {
  const detail = useProposalDetail(isExpanded ? proposal.proposal_id : null);
  const isCreation = proposal.action_kind === "create_campaign";
  const disabledByWrite = Boolean(writeDisabledReason);
  const isExpired = Date.now() >= new Date(proposal.expires_at).getTime();
  const blockedForApproval = disabledByWrite || proposal.state !== "pending" || isExpired;

  // Una propuesta que exige mirar el detalle nunca deshabilita "Aprobar" sin más — su botón
  // principal abre el detalle en su lugar (design.md §2.3, hotfix consistencia Google/Meta).
  const requiresReview = proposal.requires_expansion || isCreation;
  const opensDetailToReview = requiresReview && !isExpanded && !blockedForApproval;
  const staleAfterExpansion = requiresReview && isExpanded && (!detail.data || detail.isError || detail.isFetching || detail.data.diff.diff_hash !== proposal.diff.diff_hash);
  const planNotReady = isCreation && isExpanded && (!detail.data || !campaignPlanReady(detail.data));
  const approveDisabled = blockedForApproval || (!opensDetailToReview && (staleAfterExpansion || planNotReady));
  const approveLabel = opensDetailToReview ? "Revisar y aprobar" : "Aprobar";
  const reasonId = `disabled-reason-${proposal.proposal_id}`;
  // Siempre a la vista, nunca solo en un globo — design.md §2.3 ("siempre con el motivo a la vista").
  const approveDisabledReason = writeDisabledShortReason
    ?? (isExpired ? "Propuesta caducada." : !opensDetailToReview && (staleAfterExpansion || planNotReady) ? "Requiere confirmación en el detalle." : null);
  const urgencyClass = proposal.urgency === "critical" ? styles.rowCritical : proposal.urgency === "recommended" ? styles.rowRecommended : "";

  const dailyMoney = proposalDailyMoney(proposal) ?? proposal.estimated_impact;
  const monthlyMoney = monthlyEquivalent(dailyMoney);
  const entityDisplayName = proposalDisplayName(proposal);
  const matchedAccount = accounts.find((account) => account.platform_account_id === proposal.account_label);
  const accountName = matchedAccount ? platformAccountDisplayName(matchedAccount) : accountDisplayName(proposal.platform, proposal.account_label);
  const whereaboutsRest = [platformShortLabel(proposal.platform), accountName ?? undefined].filter(Boolean).join(" · ");
  const why = proposal.cause.replace(/\.?$/, ".") + (expiresWithin24h(proposal.expires_at) ? ` Caduca en ${expiryCountdownLabel(proposal.expires_at).replace("caduca en ", "")}.` : "");

  return (
    <>
      <div className={`${styles.row} ${urgencyClass} ${isFocused ? styles.rowFocused : ""}`} data-proposal-id={proposal.proposal_id}>
        <div
          className={`${styles.content} pressable-row`}
          onClick={() => actions.onFocus(proposal.proposal_id)}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            actions.onFocus(proposal.proposal_id);
          }}
        >
          <span className={styles.what}>{describeProposalChange(proposal)}</span>
          <span className={styles.where}>
            <span className={styles.entityName}>{entityDisplayName}</span> · {whereaboutsRest}
          </span>
          {proposal.package_id ? <PackageLinkChip packageId={proposal.package_id} /> : null}
          <span className={styles.why}>{why}</span>
        </div>

        {dailyMoney.amount === 0 ? (
          <span className={styles.money}>Sin coste extra</span>
        ) : (
          <span className={styles.money}>
            {formatSignedMoney(dailyMoney)}/día
            <br />
            {formatMoney(monthlyMoney)}/mes
          </span>
        )}

        <div className={styles.actions} onClick={(event) => event.stopPropagation()}>
          <button
            type="button"
            className={styles.approveButton}
            disabled={approveDisabled}
            aria-describedby={approveDisabled && approveDisabledReason ? reasonId : undefined}
            onClick={() => (opensDetailToReview ? actions.onToggleExpand(proposal.proposal_id) : actions.onApprove(proposal))}
          >
            {approveLabel}
          </button>
          {approveDisabled && approveDisabledReason ? (
            <span id={reasonId} className={styles.disabledReason}>{approveDisabledReason}</span>
          ) : null}
          <div className={styles.secondaryRow}>
            <button type="button" className={styles.rejectButton} disabled={disabledByWrite} aria-describedby={disabledByWrite ? reasonId : undefined} onClick={() => actions.onReject(proposal)}>
              Retirar
            </button>
            <RowMenu
              label={`Más opciones para ${entityDisplayName}`}
              disabled={Boolean(writeDisabledReason)}
              items={[
                { key: "postpone", label: "Más tarde", onSelect: () => actions.onPostpone(proposal, "tarde") },
                { key: "detail", label: "Ver detalle", onSelect: () => actions.onToggleExpand(proposal.proposal_id) },
              ]}
            />
          </div>
          <button type="button" className={styles.detailToggle} aria-expanded={isExpanded} aria-label={isExpanded ? "Colapsar" : "Detalle"} onClick={() => actions.onToggleExpand(proposal.proposal_id)}>
            {isExpanded ? "Colapsar ⌃" : "Detalle ⌄"}
          </button>
        </div>
      </div>
      {isExpanded ? <ProposalDetailPanel proposalId={proposal.proposal_id} /> : null}
    </>
  );
}
