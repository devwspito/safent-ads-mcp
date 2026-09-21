import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { describeApiError } from "@/utils/apiError";
import { ApiRequestError } from "@/api/client";
import { useMe } from "@/api/queries/auth";
import {
  useApproveProposal,
  useBatchApprove,
  usePostponeProposal,
  useProposals,
  useProposalDetail,
  useRejectProposal,
} from "@/api/queries/proposals";
import { usePlatformAccounts } from "@/api/queries/connections";
import { useFreshness } from "@/api/queries/freshness";
import { useKillSwitch } from "@/api/queries/killSwitch";
import { useApprovePackage, useRejectPackage } from "@/api/queries/packages";
import { useUndoExecution, useUndoExecutionsBatch } from "@/api/queries/executions";
import { isPackageFeedItem, type PackageFeedItem, type ProposalGroup, type ProposalItem } from "@/api/schemas/proposals";
import { ProposalGroupCard, type PostponeOption } from "@/components/proposals/ProposalGroupCard";
import { ProposalRowSkeleton } from "@/components/proposals/ProposalRowSkeleton";
import { PageHeader } from "@/components/layout/PageHeader";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { ReloadingIndicator } from "@/components/states/ReloadingIndicator";
import { StaleBanner } from "@/components/states/StaleBanner";
import { TypedConfirmDialog } from "@/components/common/TypedConfirmDialog";
import { UndoBar } from "@/components/common/UndoBar";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { useCockpitFilters } from "@/hooks/useCockpitFilters";
import { useServerGraceUndo } from "@/hooks/useServerGraceUndo";
import { describeProposalChange, expiresWithin24h, feedItemId, proposalDisplayName, quickPostponeDate } from "@/utils/proposals";
import { platformShortLabel } from "@/utils/platform";
import { formatRelativeTime, staleDataLabel, weekdayDayLabel } from "@/utils/time";
import styles from "./PropuestasPage.module.css";
import { UnconfirmedExecutions } from "@/components/proposals/UnconfirmedExecutions";
import { campaignPlanReady } from "@/api/schemas/campaignCreation";
import { LaunchPlans } from "@/components/proposals/LaunchPlans";
import { useLaunchPlans } from "@/api/queries/launchPlans";

/** `phrase` viene del servidor en el 428 (rest-api.md §Propuestas): nunca se inventa en el cliente. */
type PendingDialog =
  | { kind: "typed-confirm-approve"; proposal: ProposalItem; phrase: string }
  | { kind: "batch-reject"; group: ProposalGroup };

interface SessionTally {
  decided: number;
  startedAt: number;
}

interface DoneEntry {
  id: string;
  label: string;
  at: number;
  graceEntryId?: string;
}

interface PostponedEntry {
  id: string;
  label: string;
  until: string;
}

const PUEDE_ESPERAR_COLLAPSE_THRESHOLD = 5;

export function PropuestasPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  // Business changes discard selections, dialogs and late results from the old inbox.
  return <BusinessProposalsPage key={businessId} businessId={businessId} />;
}

function BusinessProposalsPage({ businessId }: { businessId: string }) {
  const [searchParams] = useSearchParams();
  const requestedProposal = searchParams.get("proposal_id");
  const { filters, setFilter } = useCockpitFilters();
  const launchPlans = useLaunchPlans(businessId);
  const launchCount = launchPlans.data?.items.length ?? 0;

  const proposalsQuery = useProposals({ business_id: businessId, lens: "urgency" });
  const killSwitch = useKillSwitch(businessId);
  const freshness = useFreshness(businessId);
  const accounts = usePlatformAccounts(businessId);

  const approveMutation = useApproveProposal(businessId);
  const rejectMutation = useRejectProposal(businessId);
  const postponeMutation = usePostponeProposal(businessId);
  const batchApproveMutation = useBatchApprove(businessId);
  const undoMutation = useUndoExecution(businessId);
  const undoBatchMutation = useUndoExecutionsBatch(businessId);
  const approvePackageMutation = useApprovePackage(businessId);
  const rejectPackageMutation = useRejectPackage(businessId);

  const grace = useServerGraceUndo({
    scopeKey: businessId,
    onUndo: async (executionId) => {
      await undoMutation.mutateAsync({ executionId, reason: "Deshecho desde Propuestas" });
    },
    onUndoBatch: async (executionIds) => {
      await undoBatchMutation.mutateAsync({ executionIds, reason: "Deshecho en lote desde Propuestas" });
    },
  });

  const [focusedProposalId, setFocusedProposalId] = useState<string | null>(requestedProposal);
  const [expandedProposalId, setExpandedProposalId] = useState<string | null>(requestedProposal);
  const expandedDetail = useProposalDetail(expandedProposalId);
  const [puedeEsperarExpanded, setPuedeEsperarExpanded] = useState(false);
  const [dialog, setDialog] = useState<PendingDialog | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [doneToday, setDoneToday] = useState<DoneEntry[]>([]);
  const [doneTodayExpanded, setDoneTodayExpanded] = useState(false);
  const [postponed, setPostponed] = useState<PostponedEntry[]>([]);
  const [postponedExpanded, setPostponedExpanded] = useState(false);
  const pendingAction = useRef(false);
  const [busy, setBusy] = useState(false);
  const runAction = useCallback(async (action: () => Promise<unknown>) => {
    if (pendingAction.current || proposalsQuery.isPlaceholderData || proposalsQuery.isError) return;
    pendingAction.current = true;
    setBusy(true);
    setActionError(null);
    try { await action(); }
    catch (error) { setActionError(describeApiError(error)); }
    finally { pendingAction.current = false; setBusy(false); }
  }, [proposalsQuery.isPlaceholderData, proposalsQuery.isError]);
  const sessionRef = useRef<SessionTally>({ decided: 0, startedAt: Date.now() });
  const [everHadPending, setEverHadPending] = useState(false);
  const [showSummary, setShowSummary] = useState(false);

  useEffect(() => {
    setDialog(null);
    setExpandedProposalId(requestedProposal);
    setFocusedProposalId(requestedProposal);
    setActionError(null);
    setNotice(null);
    setEverHadPending(false);
    setShowSummary(false);
    setDoneToday([]);
    setDoneTodayExpanded(false);
    setPostponed([]);
    setPostponedExpanded(false);
    sessionRef.current = { decided: 0, startedAt: Date.now() };
  }, [businessId, requestedProposal]);

  const pages = useMemo(() => proposalsQuery.data?.pages ?? [], [proposalsQuery.data]);
  const firstPage = pages[0];
  const allProposalsUnfiltered = useMemo(() => pages.flatMap((page) => page.groups).flatMap((group) => group.proposals), [pages]);

  const allGroups = useMemo(() => {
    const groups = pages.flatMap((page) => page.groups);
    if (!filters.platform) return groups;
    return groups
      .map((group) => {
        const proposals = group.proposals.filter((p) => p.platform === filters.platform);
        return { ...group, proposals, count: proposals.length,
          total_impact: { ...group.total_impact, amount: proposals.reduce((sum, p) => sum + ("estimated_impact" in p ? p.estimated_impact.amount : 0), 0) } };
      })
      .filter((group) => group.proposals.length > 0);
  }, [pages, filters.platform]);

  const totalPending = allGroups.reduce((sum, g) => sum + g.count, 0);
  const expiringSoonCount = allProposalsUnfiltered.filter((p) => expiresWithin24h(p.expires_at)).length;

  useEffect(() => {
    if (totalPending > 0) setEverHadPending(true);
    setShowSummary(everHadPending && firstPage?.pending_count === 0 && sessionRef.current.decided > 0);
  }, [totalPending, everHadPending, firstPage?.pending_count]);

  const { urgenteGroups, recomendadoGroups, puedeEsperarGroups } = useMemo(() => ({
    urgenteGroups: allGroups.filter((g) => g.proposals[0]?.urgency === "critical"),
    recomendadoGroups: allGroups.filter((g) => g.proposals[0]?.urgency === "recommended"),
    puedeEsperarGroups: allGroups.filter((g) => g.proposals[0]?.urgency === "minor"),
  }), [allGroups]);

  const puedeEsperarCount = puedeEsperarGroups.reduce((sum, g) => sum + g.count, 0);
  const puedeEsperarCollapsed = puedeEsperarCount > PUEDE_ESPERAR_COLLAPSE_THRESHOLD && !puedeEsperarExpanded;

  const visibleGroups = useMemo(
    () => (puedeEsperarCollapsed ? [...urgenteGroups, ...recomendadoGroups] : [...urgenteGroups, ...recomendadoGroups, ...puedeEsperarGroups]),
    [urgenteGroups, recomendadoGroups, puedeEsperarGroups, puedeEsperarCollapsed],
  );
  const visibleProposals = useMemo(() => visibleGroups.flatMap((g) => g.proposals), [visibleGroups]);

  // Un paquete exige detalle abierto para aprobar (T052): si solo hay uno pendiente, se lo
  // ahorramos al dueño y lo abrimos ya — una sola vez, nunca vuelve a forzarlo si lo colapsa.
  const autoExpandedPackageRef = useRef(false);
  useEffect(() => {
    if (autoExpandedPackageRef.current) return;
    const pendingPackages = visibleProposals.filter((p): p is PackageFeedItem => isPackageFeedItem(p) && p.state === "proposed");
    if (pendingPackages.length !== 1) return;
    autoExpandedPackageRef.current = true;
    setExpandedProposalId(pendingPackages[0]!.package_id);
  }, [visibleProposals]);

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = proposalsQuery;
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !hasNextPage) return;
    const observer = new IntersectionObserver((observed) => {
      if (observed[0]?.isIntersecting && !isFetchingNextPage) void fetchNextPage();
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const killSwitchEngaged = killSwitch.data?.effective.engaged ?? false;
  const isStale = freshness.data?.is_stale ?? false;
  const writeDisabledReason = killSwitchEngaged
    ? "Los cambios están parados. Reanúdalos arriba para aprobar o rechazar."
    : isStale
      ? "Los datos están desactualizados. Actualiza antes de aprobar o rechazar."
      : null;
  // Motivo corto, siempre a la vista bajo «Aprobar» — design.md §2.3, encargo (b).
  const writeDisabledShortReason = killSwitchEngaged
    ? "Cambios parados."
    : isStale && freshness.data
      ? `${staleDataLabel(freshness.data.lag_minutes)}.`
      : null;

  const focusNext = useCallback(
    (currentId: string) => {
      const index = visibleProposals.findIndex((p) => feedItemId(p) === currentId);
      const next = visibleProposals[index + 1] ?? visibleProposals[index - 1] ?? null;
      setFocusedProposalId(next ? feedItemId(next) : null);
    },
    [visibleProposals],
  );

  const bumpSession = useCallback(() => {
    sessionRef.current = { ...sessionRef.current, decided: sessionRef.current.decided + 1 };
  }, []);

  const pushDone = useCallback((entry: Omit<DoneEntry, "at">) => {
    setDoneToday((prev) => [{ ...entry, at: Date.now() }, ...prev].slice(0, 20));
  }, []);

  const doApprove = useCallback(
    async (proposal: ProposalItem, typedConfirmation?: string) => {
      const isCreation = proposal.action_kind === "create_campaign";
      const detailStale = expandedProposalId !== proposal.proposal_id || !expandedDetail.data || expandedDetail.isError || expandedDetail.isFetching || expandedDetail.data.diff.diff_hash !== proposal.diff.diff_hash;
      if (isCreation && (detailStale || !campaignPlanReady(expandedDetail.data!))) {
        throw new Error("Revisa el plan de creación vigente antes de aprobar.");
      }
      try {
        const result = await approveMutation.mutateAsync({
          proposalId: proposal.proposal_id,
          diffHash: proposal.diff.diff_hash,
          typedConfirmation,
        });
        const label = `${describeProposalChange(proposal)} · ${proposalDisplayName(proposal)}`;
        grace.enqueue({ executionId: result.execution_id, label: `Aprobado: ${proposalDisplayName(proposal)}`, deadline: new Date(result.undo_deadline ?? result.execution_scheduled_at).getTime() });
        pushDone({ id: result.execution_id, label, graceEntryId: result.execution_id });
        bumpSession();
        focusNext(proposal.proposal_id);
      } catch (error) {
        if (error instanceof ApiRequestError && error.code === "TYPED_CONFIRMATION_REQUIRED") {
          if (typedConfirmation || typeof error.details?.phrase !== "string" || !error.details.phrase) throw error;
          const phrase = error.details.phrase;
          setDialog({ kind: "typed-confirm-approve", proposal, phrase });
          return;
        }
        throw error;
      }
    },
    [approveMutation, grace, pushDone, bumpSession, focusNext, expandedProposalId, expandedDetail.data, expandedDetail.isError, expandedDetail.isFetching],
  );

  const handleApprove = useCallback(
    (proposal: ProposalItem) => {
      if (writeDisabledReason || document.querySelector('[role="dialog"]')) return;
      const isCreation = proposal.action_kind === "create_campaign";
      const detailStale = expandedProposalId !== proposal.proposal_id || !expandedDetail.data || expandedDetail.isError || expandedDetail.isFetching || expandedDetail.data.diff.diff_hash !== proposal.diff.diff_hash;
      if ((proposal.requires_expansion || isCreation) && detailStale) return;
      if (isCreation && expandedDetail.data && !campaignPlanReady(expandedDetail.data)) return;
      void runAction(() => doApprove(proposal));
    },
    [expandedProposalId, expandedDetail.data, expandedDetail.isError, expandedDetail.isFetching, doApprove, runAction, writeDisabledReason],
  );

  const handleReject = useCallback(
    async (proposal: ProposalItem) => {
      await rejectMutation.mutateAsync({ proposalId: proposal.proposal_id, diffHash: proposal.diff.diff_hash });
      pushDone({ id: `reject-${proposal.proposal_id}-${Date.now()}`, label: `Rechazada: ${proposalDisplayName(proposal)}` });
      bumpSession();
      focusNext(proposal.proposal_id);
    },
    [rejectMutation, pushDone, bumpSession, focusNext],
  );

  const handlePostpone = useCallback(
    async (proposal: ProposalItem, option: PostponeOption) => {
      const until = quickPostponeDate(option);
      await postponeMutation.mutateAsync({ proposalId: proposal.proposal_id, until });
      // Confirmación inline con la fecha, y un sitio permanente donde encontrarla — posponer
      // nunca hace que la propuesta desaparezca en silencio (encargo (7)).
      setNotice(`Pospuesta: vuelve el ${weekdayDayLabel(until)}.`);
      setPostponed((prev) => [{ id: `${proposal.proposal_id}-${Date.now()}`, label: proposalDisplayName(proposal), until }, ...prev].slice(0, 20));
      bumpSession();
      focusNext(proposal.proposal_id);
    },
    [postponeMutation, bumpSession, focusNext],
  );

  const handleBatchApprove = useCallback(
    async (group: ProposalGroup) => {
      // Un paquete nunca es `batch_eligible` (api.md §1) — ProposalGroupCard ya lo garantiza;
      // este filtro es sólo para que TypeScript sepa que aquí `proposals` son propuestas normales.
      const proposals = group.proposals.filter((item): item is ProposalItem => !isPackageFeedItem(item));
      if (writeDisabledReason || proposals.length !== group.proposals.length || proposals.some((proposal) => proposal.action_kind === "create_campaign")) return;
      const results = await batchApproveMutation.mutateAsync({
        causeKey: group.cause_key,
        items: proposals.map((p) => ({ proposalId: p.proposal_id, diffHash: p.diff.diff_hash })),
      });
      const ok = results.results.filter((r) => r.ok);
      ok.forEach((r) => {
        if (!r.execution_id || !r.execution_scheduled_at) return;
        const proposal = proposals.find((p) => p.proposal_id === r.proposal_id);
        grace.enqueue({ executionId: r.execution_id, label: `Aprobado: ${proposal?.entity_name ?? r.proposal_id}`, deadline: new Date(r.execution_scheduled_at).getTime() });
        pushDone({ id: r.execution_id, label: `${proposal ? describeProposalChange(proposal) : "Aprobada"} · ${proposal?.entity_name ?? ""}`, graceEntryId: r.execution_id });
      });
      const failed = results.results.filter((r) => !r.ok);
      sessionRef.current = { ...sessionRef.current, decided: sessionRef.current.decided + ok.length };
      setNotice(failed.length > 0 ? `${ok.length} aprobadas, ${failed.length} fallidas` : `${ok.length} aprobadas`);
    },
    [batchApproveMutation, grace, pushDone, writeDisabledReason],
  );

  const handleBatchReject = useCallback((group: ProposalGroup) => setDialog({ kind: "batch-reject", group }), []);

  const handleApprovePackage = useCallback(
    async (item: PackageFeedItem, packageHash: string) => {
      await approvePackageMutation.mutateAsync({ packageId: item.package_id, packageHash });
      pushDone({ id: `pkg-approve-${item.package_id}-${Date.now()}`, label: `${item.headline} · ${item.entity_name}` });
      bumpSession();
      focusNext(item.package_id);
    },
    [approvePackageMutation, pushDone, bumpSession, focusNext],
  );

  const handleRejectPackage = useCallback(
    async (item: PackageFeedItem) => {
      await rejectPackageMutation.mutateAsync({ packageId: item.package_id, packageHash: item.diff_hash });
      pushDone({ id: `pkg-reject-${item.package_id}-${Date.now()}`, label: `Rechazada: ${item.entity_name}` });
      bumpSession();
      focusNext(item.package_id);
    },
    [rejectPackageMutation, pushDone, bumpSession, focusNext],
  );

  const handleDialogConfirm = useCallback(
    async (reason: string) => {
      if (!dialog) return;
      if (dialog.kind === "typed-confirm-approve") {
        await doApprove(dialog.proposal, dialog.phrase);
        setDialog(null);
      } else if (dialog.kind === "batch-reject") {
        const proposals = dialog.group.proposals.filter((item): item is ProposalItem => !isPackageFeedItem(item));
        const results = await Promise.allSettled(
          proposals.map((p) => rejectMutation.mutateAsync({ proposalId: p.proposal_id, diffHash: p.diff.diff_hash, comment: reason })),
        );
        const successful = results.filter((result) => result.status === "fulfilled").length;
        sessionRef.current = { ...sessionRef.current, decided: sessionRef.current.decided + successful };
        setNotice(`${successful} rechazadas${successful < results.length ? `, ${results.length - successful} fallidas. Revisa las propuestas pendientes.` : ""}`);
        setDialog(null);
      }
    },
    [dialog, doApprove, rejectMutation],
  );

  const actions = useMemo(
    () => ({
      onFocus: setFocusedProposalId,
      onToggleExpand: (id: string) => setExpandedProposalId((prev) => (prev === id ? null : id)),
      onApprove: handleApprove,
      onReject: (proposal: ProposalItem) => void runAction(() => handleReject(proposal)),
      onPostpone: (proposal: ProposalItem, option: PostponeOption) => void runAction(() => handlePostpone(proposal, option)),
      onBatchApprove: (group: ProposalGroup) => void runAction(() => handleBatchApprove(group)),
      onBatchReject: handleBatchReject,
      onApprovePackage: (item: PackageFeedItem, packageHash: string) => void runAction(() => handleApprovePackage(item, packageHash)),
      onRejectPackage: (item: PackageFeedItem) => void runAction(() => handleRejectPackage(item)),
    }),
    [handleApprove, handleReject, handlePostpone, handleBatchApprove, handleBatchReject, handleApprovePackage, handleRejectPackage, runAction],
  );

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || event.repeat || dialog || pendingAction.current || document.querySelector('[role="dialog"]')) return;

      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        if (grace.entries.length === 0) return;
        event.preventDefault();
        void runAction(() => grace.undo(grace.entries[grace.entries.length - 1]!.id));
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      const target = event.target;
      if (target instanceof HTMLElement && (target.isContentEditable || target.closest("button, a, [role=button]"))) return;
      if (target instanceof HTMLElement && (target.tagName === "INPUT" || target.tagName === "SELECT" || target.tagName === "TEXTAREA")) return;
      const focused = visibleProposals.find((p) => feedItemId(p) === focusedProposalId);
      if (!focused && !"jk".includes(event.key.toLowerCase())) return;

      if (event.key.toLowerCase() === "j") {
        event.preventDefault();
        const index = focused ? visibleProposals.findIndex((p) => feedItemId(p) === feedItemId(focused)) : -1;
        const next = visibleProposals[index + 1];
        if (next) setFocusedProposalId(feedItemId(next));
      } else if (event.key.toLowerCase() === "k") {
        event.preventDefault();
        const index = focused ? visibleProposals.findIndex((p) => feedItemId(p) === feedItemId(focused)) : visibleProposals.length;
        const prev = visibleProposals[index - 1];
        if (prev) setFocusedProposalId(feedItemId(prev));
      } else if (!focused) {
        return;
      } else if (event.key === "Enter") {
        const id = feedItemId(focused);
        setExpandedProposalId((prev) => (prev === id ? null : id));
      } else if (isPackageFeedItem(focused)) {
        // Un paquete publica gasto real y su aprobación exige el detalle abierto (T052):
        // sin atajo de una sola tecla, a diferencia de una propuesta normal.
        return;
      } else if (event.key.toLowerCase() === "a") {
        handleApprove(focused);
      } else if (event.key.toLowerCase() === "r") {
        void runAction(() => handleReject(focused));
      } else if (event.key.toLowerCase() === "p") {
        void runAction(() => handlePostpone(focused, "tarde"));
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [visibleProposals, focusedProposalId, handleApprove, handleReject, handlePostpone, runAction, dialog, grace]);

  const noAccounts = accounts.data?.items.length === 0;
  // Vacío POR EL FILTRO (hay cosas en la otra plataforma) ≠ vacío de verdad (nada en ningún sitio).
  const activePlatform = filters.platform === "meta" ? "meta" : filters.platform === "google" ? "google" : null;
  const otherPlatform = activePlatform === "meta" ? "google" : "meta";
  const emptyByFilter = activePlatform !== null && totalPending === 0 && allProposalsUnfiltered.length > 0;

  return (
    <div>
      <PageHeader
        title="Propuestas"
        actions={
          <div className={styles.toolbar}>
            <label className="visually-hidden" htmlFor="propuestas-platform-filter">
              Filtrar por plataforma
            </label>
            <select
              id="propuestas-platform-filter"
              className={styles.select}
              value={filters.platform ?? ""}
              onChange={(e) => setFilter("platform", e.target.value || undefined)}
            >
              <option value="">Todas</option>
              <option value="google">Google</option>
              <option value="meta">Meta</option>
            </select>
            <Link to="/propuestas/historial" className={styles.historialLink}>
              Historial
            </Link>
          </div>
        }
      />
      <Link to={`/trabajo?business_id=${businessId}`}>← Espacio de trabajo</Link>
      {requestedProposal && expandedDetail.isLoading && <p role="status">Cargando la decisión solicitada…</p>}
      {requestedProposal && expandedDetail.isError && <p role="alert">No se pudo cargar la decisión solicitada. <button onClick={() => void expandedDetail.refetch()}>Reintentar lectura</button></p>}
      {requestedProposal && expandedDetail.data && !allProposalsUnfiltered.some(item => feedItemId(item) === requestedProposal) && <ProposalGroupCard
        businessId={businessId}
        group={{ group_kind: "cause", cause_key: `selected:${requestedProposal}`, cause: "Decisión del proyecto", count: 1, total_impact: expandedDetail.data.estimated_impact, batch_eligible: false, closes_at: null, proposals: [expandedDetail.data] }}
        focusedProposalId={focusedProposalId} expandedProposalId={expandedProposalId}
        writeDisabledReason={writeDisabledReason} writeDisabledShortReason={writeDisabledShortReason}
        accounts={accounts.data?.items ?? []} actions={actions}
      />}
      <p className={styles.context}>
        {totalPending > 0
          ? `${totalPending} ${totalPending === 1 ? "cosa" : "cosas"} por decidir${expiringSoonCount > 0 ? ` · ${expiringSoonCount} caduca${expiringSoonCount === 1 ? "" : "n"} hoy` : ""}`
          : launchCount > 0 ? `${launchCount} ${launchCount === 1 ? "plan disponible" : "planes disponibles"} · revisa el detalle antes de aprobar`
            : launchPlans.isLoading || launchPlans.isError ? "Consultando planes de lanzamiento"
              : "Nada pendiente ahora mismo"}
      </p>
      <LaunchPlans businessId={businessId} />

      {isStale && !killSwitchEngaged ? <StaleBanner lagMinutes={freshness.data!.lag_minutes} /> : null}
      {actionError ? <p role="alert" className={styles.actionError}>{actionError}</p> : null}
      <UnconfirmedExecutions businessId={businessId} />

      <fieldset className={styles.decisionScope} disabled={busy || proposalsQuery.isPlaceholderData || proposalsQuery.isError}>
        <legend className="visually-hidden">Decisiones pendientes</legend>
        <QueryBoundary
          isLoading={proposalsQuery.isLoading}
          isError={proposalsQuery.isError}
          error={proposalsQuery.error}
          onRetry={() => void proposalsQuery.refetch()}
          data={firstPage}
          isEmpty={() => totalPending === 0 && launchCount === 0 && !launchPlans.isLoading && !launchPlans.isError}
          emptyTitle={
            noAccounts
              ? "Todavía no hay nada que proponerte"
              : emptyByFilter
                ? `Nada en ${platformShortLabel(activePlatform!)}`
                : showSummary
                  ? "Bandeja al día"
                  : "Nada que decidir"
          }
          emptyBody={
            noAccounts
              ? "No hay ninguna cuenta conectada."
              : emptyByFilter
                ? `Hay ${allProposalsUnfiltered.length} ${allProposalsUnfiltered.length === 1 ? "propuesta" : "propuestas"} en ${platformShortLabel(otherPlatform)}.`
                : showSummary
                  ? sessionSummaryText(sessionRef.current)
                  : "Safent vigila tus campañas y te avisará aquí cuando convenga cambiar algo."
          }
          emptyAction={
            noAccounts ? (
              <Link to="/ajustes" className={styles.emptyAction}>Conectar una cuenta</Link>
            ) : emptyByFilter ? (
              <button type="button" className={styles.emptyAction} onClick={() => setFilter("platform", undefined)}>Ver todas</button>
            ) : showSummary ? (
              <button type="button" className={styles.emptyAction} onClick={() => setDoneTodayExpanded(true)}>Ver lo que has hecho</button>
            ) : (
              <Link to="/resultados" className={styles.emptyAction}>Ver resultados</Link>
            )
          }
          skeleton={<><ProposalRowSkeleton /><ProposalRowSkeleton /><ProposalRowSkeleton /></>}
        >
          {() => (
            <>
              {proposalsQuery.isFetching && !proposalsQuery.isLoading && !proposalsQuery.isFetchingNextPage ? <ReloadingIndicator /> : null}

              {notice ? (
                <div className={styles.notice} role="status">
                  {notice}
                  <button type="button" className={styles.noticeDismiss} onClick={() => setNotice(null)}>
                    Cerrar
                  </button>
                </div>
              ) : null}

              {urgenteGroups.length > 0 ? (
                <>
                  <h2 className={`${styles.sectionTitle} ${styles.sectionUrgente}`}>Urgente</h2>
                  {urgenteGroups.map((group) => (
                    <ProposalGroupCard key={group.cause_key} businessId={businessId} group={group} focusedProposalId={focusedProposalId} expandedProposalId={expandedProposalId} writeDisabledReason={writeDisabledReason} writeDisabledShortReason={writeDisabledShortReason} accounts={accounts.data?.items ?? []} actions={actions} />
                  ))}
                </>
              ) : null}

              {recomendadoGroups.length > 0 ? (
                <>
                  <h2 className={styles.sectionTitle}>Recomendado</h2>
                  {recomendadoGroups.map((group) => (
                    <ProposalGroupCard key={group.cause_key} businessId={businessId} group={group} focusedProposalId={focusedProposalId} expandedProposalId={expandedProposalId} writeDisabledReason={writeDisabledReason} writeDisabledShortReason={writeDisabledShortReason} accounts={accounts.data?.items ?? []} actions={actions} />
                  ))}
                </>
              ) : null}

              {puedeEsperarGroups.length > 0 ? (
                <>
                  <h2 className={styles.sectionTitle}>
                    {/* Sin nada urgente ni recomendado a la vista, "puede esperar" suena a excusa — es lo único pendiente. */}
                    {urgenteGroups.length === 0 && recomendadoGroups.length === 0 ? "Pendientes" : "Puede esperar"} ({puedeEsperarCount})
                    {puedeEsperarCount > PUEDE_ESPERAR_COLLAPSE_THRESHOLD ? (
                      <button type="button" className={styles.collapseToggle} onClick={() => setPuedeEsperarExpanded((v) => !v)}>
                        {puedeEsperarCollapsed ? "Ver todas ⌄" : "Ver menos ⌃"}
                      </button>
                    ) : null}
                  </h2>
                  {!puedeEsperarCollapsed
                    ? puedeEsperarGroups.map((group) => (
                        <ProposalGroupCard key={group.cause_key} businessId={businessId} group={group} focusedProposalId={focusedProposalId} expandedProposalId={expandedProposalId} writeDisabledReason={writeDisabledReason} writeDisabledShortReason={writeDisabledShortReason} accounts={accounts.data?.items ?? []} actions={actions} />
                      ))
                    : null}
                </>
              ) : null}

              <div ref={sentinelRef} aria-hidden="true" />
              {proposalsQuery.isFetchingNextPage ? <p role="status" className={styles.loadingMore}>Cargando más…</p> : null}
            </>
          )}
        </QueryBoundary>
      </fieldset>

      <PostponedSection entries={postponed} expanded={postponedExpanded} onToggleExpanded={() => setPostponedExpanded((v) => !v)} />

      <DoneTodaySection entries={doneToday} expanded={doneTodayExpanded} onToggleExpanded={() => setDoneTodayExpanded((v) => !v)} grace={grace} onUndo={(id) => void runAction(() => grace.undo(id))} />

      <UndoBar entries={grace.entries} secondsLeft={grace.secondsLeft} onUndo={(id) => void runAction(() => grace.undo(id))} onUndoAll={() => void runAction(() => grace.undoAll())} />

      {dialog ? <PendingDialogView dialog={dialog} onConfirm={handleDialogConfirm} onClose={() => setDialog(null)} /> : null}
    </div>
  );
}

function sessionSummaryText(tally: SessionTally): string {
  const minutes = Math.max(1, Math.round((Date.now() - tally.startedAt) / 60_000));
  return `Has decidido ${tally.decided} ${tally.decided === 1 ? "cosa" : "cosas"} · ${minutes} min`;
}

interface PostponedSectionProps {
  entries: PostponedEntry[];
  expanded: boolean;
  onToggleExpanded: () => void;
}

/** «Pospuestas (N)» — nada desaparece en silencio al posponer (encargo (7)). */
function PostponedSection({ entries, expanded, onToggleExpanded }: PostponedSectionProps) {
  if (entries.length === 0) return null;
  return (
    <section className={styles.doneToday} aria-label="Pospuestas">
      <button type="button" className={styles.doneTodayToggle} aria-expanded={expanded} onClick={onToggleExpanded}>
        Pospuestas ({entries.length}) {expanded ? "⌃" : "⌄"}
      </button>
      {expanded ? (
        <ul className={styles.doneTodayList}>
          {entries.slice(0, 8).map((entry) => (
            <li key={entry.id} className={styles.doneTodayItem}>
              <span>{entry.label} · vuelve el {weekdayDayLabel(entry.until)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

interface DoneTodaySectionProps {
  entries: DoneEntry[];
  expanded: boolean;
  onToggleExpanded: () => void;
  grace: ReturnType<typeof useServerGraceUndo>;
  onUndo: (id: string) => void;
}

/** Sección plegada al final — panel-interaction-spec.md §3.4: abierta solo si hay algo con deshacer vivo. */
function DoneTodaySection({ entries, expanded, onToggleExpanded, grace, onUndo }: DoneTodaySectionProps) {
  if (entries.length === 0) return null;
  const open = expanded || grace.entries.length > 0;

  return (
    <section className={styles.doneToday} aria-label="Hecho hoy">
      <button type="button" className={styles.doneTodayToggle} aria-expanded={open} onClick={onToggleExpanded}>
        Hecho hoy ({entries.length}) {open ? "⌃" : "⌄"}
      </button>
      {open ? (
        <>
          <ul className={styles.doneTodayList}>
            {entries.slice(0, 8).map((entry) => {
              const graceEntry = entry.graceEntryId ? grace.entries.find((g) => g.id === entry.graceEntryId) : undefined;
              return (
                <li key={entry.id} className={styles.doneTodayItem}>
                  <span>{entry.label} · {formatRelativeTime(new Date(entry.at).toISOString())}</span>
                  {graceEntry ? (
                    <button type="button" className={styles.doneTodayUndo} onClick={() => onUndo(graceEntry.id)}>
                      Deshacer ({grace.secondsLeft(graceEntry)} s)
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
          <Link to="/propuestas/historial" className={styles.doneTodayLink}>Historial</Link>
        </>
      ) : null}
    </section>
  );
}

function PendingDialogView({ dialog, onConfirm, onClose }: { dialog: PendingDialog; onConfirm: (reason: string) => void; onClose: () => void }) {
  if (dialog.kind === "typed-confirm-approve") {
    return (
      <TypedConfirmDialog
        title="Confirmar aprobación"
        description={`${dialog.proposal.entity_name} · ${dialog.proposal.diff.valor_actual} → ${dialog.proposal.diff.valor_propuesto}. El servidor exige confirmación tecleada para este cambio.`}
        confirmLabel="Sí, aprobar"
        confirmWord={dialog.phrase}
        onConfirm={onConfirm}
        onClose={onClose}
      />
    );
  }
  return (
    <TypedConfirmDialog
      title="Rechazar todas"
      description={`Vas a rechazar las ${dialog.group.count} propuestas de "${dialog.group.cause}". Escribe el motivo una vez.`}
      confirmLabel="Rechazar todas"
      danger
      reasonRequired
      reasonLabel="Motivo"
      onConfirm={onConfirm}
      onClose={onClose}
    />
  );
}
