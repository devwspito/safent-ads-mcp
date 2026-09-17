import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useMe } from "@/api/queries/auth";
import { usePlatformAccounts } from "@/api/queries/connections";
import { useCockpit } from "@/api/queries/cockpit";
import type { CockpitView, TickerRow } from "@/api/schemas/cockpit";
import type { PlatformAccount } from "@/api/schemas/connections";
import { useUndoExecution } from "@/api/queries/executions";
import { usePauseEntity, useResumeEntity, useDeleteEntity } from "@/api/queries/entityLifecycle";
import { useKillSwitch, useSetKillSwitch } from "@/api/queries/killSwitch";
import { AccountGroup } from "@/components/campaigns/AccountGroup";
import type { CampaignRowActions } from "@/components/campaigns/CampaignRow";
import { CampaignStatusTabs, type CampaignStatusFilter } from "@/components/campaigns/CampaignStatusTabs";
import { DeleteCampaignSheet } from "@/components/campaigns/DeleteCampaignSheet";
import { UnidentifiedAccountGroup } from "@/components/campaigns/UnidentifiedAccountGroup";
import { BrakeDialog } from "@/components/layout/BrakeDialog";
import { PageHeader } from "@/components/layout/PageHeader";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { ReloadingIndicator } from "@/components/states/ReloadingIndicator";
import { StaleBanner } from "@/components/states/StaleBanner";
import { UndoBar } from "@/components/common/UndoBar";
import { EmptyState } from "@/components/states/EmptyState";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { useServerGraceUndo } from "@/hooks/useServerGraceUndo";
import { describeApiError } from "@/utils/apiError";
import { accountRowState } from "@/utils/accountStatus";
import { platformLabel } from "@/utils/platform";
import { matchesSearch } from "@/utils/search";
import { staleDataLabel } from "@/utils/time";
import styles from "./CampanasPage.module.css";

type ActionBusy = "pausing" | "resuming" | undefined;

export function CampanasPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return <BusinessCampanasPage key={businessId} businessId={businessId} />;
}

function BusinessCampanasPage({ businessId }: { businessId: string }) {
  const queryClient = useQueryClient();
  const cockpit = useCockpit(businessId, "today");
  const accounts = usePlatformAccounts(businessId);
  const killSwitch = useKillSwitch(businessId);
  const setKillSwitch = useSetKillSwitch(businessId);

  const pauseMutation = usePauseEntity(businessId);
  const resumeMutation = useResumeEntity(businessId);
  const deleteMutation = useDeleteEntity(businessId);
  const undoMutation = useUndoExecution(businessId);

  const grace = useServerGraceUndo({
    scopeKey: businessId,
    onUndo: async (executionId) => {
      await undoMutation.mutateAsync({ executionId, reason: "Deshecho desde Campañas" });
      void queryClient.invalidateQueries({ queryKey: ["cockpit", businessId] });
    },
  });

  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [expandedEntityRef, setExpandedEntityRef] = useState<string | null>(null);
  const [actionBusyByRef, setActionBusyByRef] = useState<Record<string, ActionBusy>>({});
  const [rowError, setRowError] = useState<{ entityRef: string; message: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<TickerRow | null>(null);
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);
  const [brakeDialogOpen, setBrakeDialogOpen] = useState(false);

  function setRowStatus(entityRef: string, status: "ACTIVE" | "PAUSED") {
    queryClient.setQueryData<CockpitView>(["cockpit", businessId, "today"], (prev) =>
      prev ? { ...prev, rows: prev.rows.map((row) => (row.entity_ref === entityRef ? { ...row, status } : row)) } : prev,
    );
  }

  const handlePause = useCallback(
    async (row: TickerRow) => {
      if (actionBusyByRef[row.entity_ref]) return;
      setActionBusyByRef((prev) => ({ ...prev, [row.entity_ref]: "pausing" }));
      setRowError(null);
      setRowStatus(row.entity_ref, "PAUSED");
      try {
        const result = await pauseMutation.mutateAsync(row.entity_ref);
        grace.enqueue({
          executionId: result.execution_id,
          label: `Campaña pausada · ${row.name}`,
          deadline: new Date(result.undo_deadline ?? Date.now() + 15_000).getTime(),
        });
      } catch (error) {
        setRowStatus(row.entity_ref, "ACTIVE");
        setRowError({ entityRef: row.entity_ref, message: `No se pudo pausar: ${describeApiError(error)}` });
      } finally {
        setActionBusyByRef((prev) => ({ ...prev, [row.entity_ref]: undefined }));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actionBusyByRef, pauseMutation, grace, businessId],
  );

  const handleResume = useCallback(
    async (row: TickerRow) => {
      if (actionBusyByRef[row.entity_ref]) return;
      setActionBusyByRef((prev) => ({ ...prev, [row.entity_ref]: "resuming" }));
      setRowError(null);
      setRowStatus(row.entity_ref, "ACTIVE");
      try {
        const result = await resumeMutation.mutateAsync(row.entity_ref);
        grace.enqueue({
          executionId: result.execution_id,
          label: `Campaña reanudada · ${row.name}`,
          deadline: new Date(result.undo_deadline ?? Date.now() + 15_000).getTime(),
        });
      } catch (error) {
        setRowStatus(row.entity_ref, "PAUSED");
        setRowError({ entityRef: row.entity_ref, message: `No se pudo reanudar: ${describeApiError(error)}` });
      } finally {
        setActionBusyByRef((prev) => ({ ...prev, [row.entity_ref]: undefined }));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actionBusyByRef, resumeMutation, grace, businessId],
  );

  async function handleDeleteConfirmed(row: TickerRow) {
    await deleteMutation.mutateAsync(row.entity_ref);
    setDeleteTarget(null);
    setDeleteNotice(`«${row.name}» eliminada.`);
    window.setTimeout(() => setDeleteNotice(null), 4_000);
  }

  const rowActions: CampaignRowActions = useMemo(
    () => ({
      onToggleExpand: (entityRef) => setExpandedEntityRef((prev) => (prev === entityRef ? null : entityRef)),
      onPause: (row) => void handlePause(row),
      onResume: (row) => void handleResume(row),
      onRequestDelete: (row) => setDeleteTarget(row),
    }),
    [handlePause, handleResume],
  );

  const isLoading = cockpit.isLoading || accounts.isLoading;
  const isError = cockpit.isError || accounts.isError;
  const combined = cockpit.data && accounts.data ? { cockpit: cockpit.data, accounts: accounts.data.items } : undefined;
  const noAccounts = (accounts.data?.items.length ?? 0) === 0;

  const allCampaignRows = (cockpit.data?.rows ?? []).filter((row) => row.level === "campaign");
  const activeTotal = allCampaignRows.filter((row) => row.status === "ACTIVE").length;
  const pausedTotal = allCampaignRows.filter((row) => row.status === "PAUSED").length;
  const isStale = cockpit.data?.freshness.is_stale ?? false;
  const staleReason = isStale ? `${staleDataLabel(cockpit.data!.freshness.lag_minutes)}.` : null;

  const estadoParam = searchParams.get("estado");
  // Por defecto Activas si hay alguna; si no, Todas — no tiene sentido aterrizar en una pestaña vacía.
  const estado: CampaignStatusFilter =
    estadoParam === "activas" || estadoParam === "pausadas" || estadoParam === "todas"
      ? estadoParam
      : activeTotal > 0
        ? "activas"
        : "todas";
  const setEstado = useCallback(
    (next: CampaignStatusFilter) => setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      params.set("estado", next);
      return params;
    }, { replace: true }),
    [setSearchParams],
  );

  const searchedRows = allCampaignRows.filter((row) => matchesSearch(row.name, search));
  const matchingRows = searchedRows.filter((row) =>
    estado === "todas" ? true : estado === "activas" ? row.status === "ACTIVE" : row.status === "PAUSED",
  );

  const byPlatform = useMemo(() => {
    const platforms: Array<{ platform: "google" | "meta"; accounts: Array<{ account: PlatformAccount; rows: TickerRow[] }> }> = [];
    for (const account of combined?.accounts ?? []) {
      const rows = matchingRows.filter((row) => row.platform_account_id === account.platform_account_id);
      if (rows.length === 0) continue;
      let bucket = platforms.find((p) => p.platform === account.platform);
      if (!bucket) {
        bucket = { platform: account.platform, accounts: [] };
        platforms.push(bucket);
      }
      bucket.accounts.push({ account, rows });
    }
    return platforms;
  }, [combined?.accounts, matchingRows]);

  // Hotfix 0.2.20 Bug C: a row whose `platform_account_id` matches none of
  // `/platform-accounts` must still be visible, never silently dropped.
  const unidentifiedRows = useMemo(() => {
    const knownAccountIds = new Set((combined?.accounts ?? []).map((account) => account.platform_account_id));
    return matchingRows.filter((row) => !knownAccountIds.has(row.platform_account_id));
  }, [combined?.accounts, matchingRows]);

  return (
    <div>
      <PageHeader
        title="Campañas"
        actions={
          <div className={styles.toolbar}>
            <label className="visually-hidden" htmlFor="campanas-search">
              Buscar campaña
            </label>
            <input
              id="campanas-search"
              type="search"
              className={styles.search}
              placeholder="Buscar campaña"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
        }
      />
      <p className={styles.context}>
        {allCampaignRows.length} {allCampaignRows.length === 1 ? "campaña" : "campañas"} en {combined?.accounts.length ?? 0}{" "}
        {(combined?.accounts.length ?? 0) === 1 ? "cuenta" : "cuentas"}
      </p>

      <CampaignStatusTabs
        value={estado}
        onChange={setEstado}
        counts={{ activas: activeTotal, pausadas: pausedTotal, todas: allCampaignRows.length }}
        listId="campanas-list"
      />

      {staleReason ? <StaleBanner lagMinutes={cockpit.data!.freshness.lag_minutes} /> : null}
      {rowError ? (
        <p role="alert" className={styles.actionError}>
          {rowError.message}{" "}
          <button type="button" className={styles.clearSearch} onClick={() => setRowError(null)}>
            Cerrar
          </button>
        </p>
      ) : null}
      {deleteNotice ? <p role="status" className={styles.actionError}>{deleteNotice}</p> : null}

      <div id="campanas-list" role="tabpanel" aria-label={`Campañas: ${estado}`}>
      <QueryBoundary
        isLoading={isLoading}
        isError={isError}
        error={cockpit.error ?? accounts.error}
        onRetry={() => {
          void cockpit.refetch();
          void accounts.refetch();
        }}
        data={combined}
        isEmpty={(data) => noAccounts || data.cockpit.rows.filter((row) => row.level === "campaign").length === 0}
        emptyTitle={noAccounts ? "Todavía no hay campañas que mostrar" : "Estas cuentas no tienen campañas todavía."}
        emptyBody={noAccounts ? "No hay ninguna cuenta conectada." : undefined}
        emptyAction={noAccounts ? <Link className={styles.emptyAction} to="/ajustes">Conectar una cuenta</Link> : undefined}
      >
        {() => (
          <>
            {cockpit.isFetching && !cockpit.isLoading ? <ReloadingIndicator /> : null}

            {matchingRows.length === 0 && search.trim() ? (
              <p className={styles.context}>
                Ninguna campaña coincide con «{search}». <button type="button" className={styles.clearSearch} onClick={() => setSearch("")}>Borrar búsqueda</button>
              </p>
            ) : matchingRows.length === 0 && estado !== "todas" ? (
              <EmptyState
                title={estado === "activas" ? "Ninguna campaña activa" : "Ninguna campaña pausada"}
                body="Prueba a ver todas las campañas de este negocio."
                action={
                  <button type="button" className={styles.emptyAction} onClick={() => setEstado("todas")}>
                    Ver todas
                  </button>
                }
              />
            ) : (
              byPlatform.map(({ platform, accounts: accountGroups }) => (
                <div key={platform}>
                  <h2 className={styles.platformHeading}>{platformLabel(platform)}</h2>
                  {accountGroups.map(({ account, rows }) => (
                    <AccountGroup
                      key={account.platform_account_id}
                      account={account}
                      state={accountRowState(account, killSwitch.data?.by_account.find((a) => a.platform_account_id === account.platform_account_id)?.engaged ?? false)}
                      rows={rows}
                      expandedEntityRef={expandedEntityRef}
                      actionBusyByRef={actionBusyByRef}
                      staleReason={staleReason}
                      onResumeAccount={() => setBrakeDialogOpen(true)}
                      actions={rowActions}
                    />
                  ))}
                </div>
              ))
            )}
            <UnidentifiedAccountGroup
              rows={unidentifiedRows}
              expandedEntityRef={expandedEntityRef}
              actionBusyByRef={actionBusyByRef}
              actions={rowActions}
            />
          </>
        )}
      </QueryBoundary>
      </div>

      <UndoBar entries={grace.entries} secondsLeft={grace.secondsLeft} onUndo={(id) => void grace.undo(id)} onUndoAll={() => void grace.undoAll()} />

      {deleteTarget ? (
        <DeleteCampaignSheet
          campaignName={deleteTarget.name}
          platformLabel={platformLabel(deleteTarget.platform)}
          onDelete={() => handleDeleteConfirmed(deleteTarget)}
          onPauseInstead={() => {
            const row = deleteTarget;
            setDeleteTarget(null);
            void handlePause(row);
          }}
          onClose={() => setDeleteTarget(null)}
        />
      ) : null}

      {brakeDialogOpen ? (
        <BrakeDialog
          killSwitch={killSwitch.data}
          onClose={() => setBrakeDialogOpen(false)}
          onEngage={async (input) => {
            await setKillSwitch.mutateAsync({ scope_kind: input.scope_kind, scope_id: input.scope_kind === "business" ? businessId : null, mode: input.mode, engaged: true, reason: input.reason });
            setBrakeDialogOpen(false);
          }}
          onRelease={async (item, reason) => {
            await setKillSwitch.mutateAsync({
              scope_kind: item.scope_kind,
              scope_id: item.scope_id,
              mode: item.mode,
              engaged: false,
              reason: reason || "Reanudado desde Campañas",
              typed_confirmation: "REACTIVAR",
            });
            setBrakeDialogOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}
