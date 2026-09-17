/** `GET /badges` — contadores derivados de los mismos fixtures que alimentan cada vista (rest-api.md §Auditoría y salud). */
import { listPlatformAccounts } from "./connections";
import { listCreatives } from "./creatives";
import { getKillSwitchState } from "./killSwitch";
import { listProposalGroups } from "./proposals";
import { buildSignalsResponse } from "./signals";

export function buildBadges(businessId: string, signalsSince?: string) {
  const proposals = listProposalGroups("urgency");
  const criticalCount = proposals.groups.flatMap((group) => group.proposals).filter((p) => p.urgency === "critical").length;

  const pendingCreatives = listCreatives({ pendingApproval: true });

  const since = signalsSince ?? new Date(Date.now() - 24 * 3_600_000).toISOString();
  const signals = buildSignalsResponse({});
  const newSince = signals.items.filter((signal) => signal.emitted_at >= since).length;

  const accounts = listPlatformAccounts().items;
  const level =
    accounts.some((a) => a.status === "SUSPENDED" || a.token.health === "expired" || a.token.health === "revoked")
      ? ("error" as const)
      : accounts.some((a) => a.status !== "ACTIVE" || a.token.health === "expiring_soon")
        ? ("warn" as const)
        : ("ok" as const);
  const reason = level === "ok" ? null : "Alguna cuenta conectada necesita atención — revisa Conexiones.";

  const killSwitch = getKillSwitchState(businessId);

  return {
    proposals: { pending: proposals.pending_count, critical: criticalCount, deferred: proposals.deferred_count },
    creatives: { pending_approval: pendingCreatives.items.length },
    signals: { new_since: newSince, since },
    connections: { level, reason },
    brake: { engaged: killSwitch.effective.engaged, mode: killSwitch.effective.mode },
  };
}
