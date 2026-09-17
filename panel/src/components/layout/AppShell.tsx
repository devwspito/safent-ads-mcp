import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ApiRequestError, onUnauthorized } from "@/api/client";
import { ErrorState } from "@/components/states/ErrorState";
import { PageLoading } from "@/components/states/PageLoading";
import { RouteErrorBoundary } from "@/components/states/RouteErrorBoundary";
import { describeApiError } from "@/utils/apiError";
import { useBadges } from "@/api/queries/badges";
import { useMe } from "@/api/queries/auth";
import type { MeResponse } from "@/api/schemas";
import { BusinessOnboarding } from "@/components/onboarding/BusinessOnboarding";
import { useFreshness } from "@/api/queries/freshness";
import { useKillSwitch, useSetKillSwitch } from "@/api/queries/killSwitch";
import { useSettings } from "@/api/queries/settings";
import { useApplyTheme } from "@/hooks/useApplyTheme";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { MAIN_ROUTES } from "@/routes/routeConfig";
import { deriveNavBadges } from "@/utils/navBadges";
import { KillSwitchBanner } from "@/components/states/KillSwitchBanner";
import { BottomTabBar } from "./BottomTabBar";
import { BrakeDialog } from "./BrakeDialog";
import { CommandPalette } from "./CommandPalette";
import { ShortcutsHelp } from "./ShortcutsHelp";
import { TopBar } from "./TopBar";
import styles from "./AppShell.module.css";

type OverlayLayer = "palette" | "help" | "brake" | null;

/** Autenticación + navegación + atajos globales — panel-interaction-spec.md §1. */
export function AppShell() {
  const session = useMe();
  const { data: me, isLoading } = session;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  useEffect(() => onUnauthorized(() => {
    queryClient.setQueryData(["auth", "me"], undefined);
    navigate("/login", { replace: true });
  }), [navigate, queryClient]);

  if (isLoading) return <PageLoading label="Comprobando tu sesión…" />;
  if (session.isError && !(session.error instanceof ApiRequestError && session.error.status === 401)) {
    return <ErrorState message={describeApiError(session.error)} onRetry={() => void session.refetch()} />;
  }
  if (!me) return <Navigate to="/login" replace />;
  if (me.businesses.length === 0) return <BusinessOnboarding key={me.owner_id} ownerId={me.owner_id} onReady={(verified) => {
    const business = verified.businesses[0];
    if (!business || verified.owner_id !== me.owner_id) return;
    navigate(`/ajustes?business_id=${encodeURIComponent(business.business_id)}`, { replace: true });
    queryClient.setQueryData(["auth", "me"], verified);
  }} />;
  return <BusinessShell key={me.owner_id} me={me} />;
}

/** Business-scoped queries and shortcuts mount only after a verified session has a business. */
function BusinessShell({ me }: { me: MeResponse }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [layer, setLayer] = useState<OverlayLayer>(null);

  useEffect(() => {
    const input = (event: Event) => { document.documentElement.dataset.adsInput = event.type === 'keydown' ? 'keyboard' : 'pointer'; };
    const visibility = () => { document.documentElement.dataset.adsVisibility = document.hidden ? 'hidden' : 'visible'; };
    visibility();
    document.addEventListener('keydown', input, true);
    document.addEventListener('pointerdown', input, true);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      document.removeEventListener('keydown', input, true);
      document.removeEventListener('pointerdown', input, true);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, []);

  useEffect(() => {
    if (document.documentElement.dataset.adsInput === 'keyboard') document.getElementById('main-content')?.focus();
  }, [location.pathname]);

  const { businessId, setBusinessId } = useBusinessFilter(me?.businesses);
  const freshness = useFreshness(businessId);
  const killSwitch = useKillSwitch(businessId);
  const setKillSwitch = useSetKillSwitch(businessId);
  const settings = useSettings(businessId);
  useApplyTheme(settings.data?.theme);
  const badgesQuery = useBadges(businessId);
  const navBadges = useMemo(() => deriveNavBadges(badgesQuery.data), [badgesQuery.data]);

  const handleNavigate = useCallback(
    (index: number) => {
      const route = MAIN_ROUTES[index];
      if (route) navigate(route.path);
    },
    [navigate],
  );

  useKeyboardShortcuts({
    onNavigate: handleNavigate,
    onOpenPalette: () => setLayer("palette"),
    onOpenHelp: () => setLayer("help"),
    onOpenBrake: () => setLayer("brake"),
    onEscape: () => { if (!setKillSwitch.isPending) setLayer(null); },
  });

  return (
    <div className={styles.shell}>
      <a href="#main-content" className="skip-link" onClick={event => { event.preventDefault(); document.getElementById('main-content')?.focus(); }}>
        Ir al contenido principal
      </a>
      {killSwitch.data?.effective.engaged ? (
        <KillSwitchBanner
          engagedAt={killSwitch.data.effective.engaged_at ?? new Date().toISOString()}
          reason={killSwitch.data.effective.reason}
          onRequestRelease={() => setLayer("brake")}
        />
      ) : null}
      <TopBar
        businesses={me.businesses}
        businessId={businessId}
        onChangeBusiness={setBusinessId}
        lagMinutes={freshness.data?.lag_minutes}
        noData={freshness.data?.no_data}
        killSwitch={killSwitch.data}
        onOpenBrake={() => setLayer("brake")}
        onOpenHelp={() => setLayer("help")}
        badges={navBadges}
      />
      <div className={styles.body}>
        <main id="main-content" className={styles.main} tabIndex={-1}>
          <RouteErrorBoundary key={`${businessId}:${location.pathname}`}>
          <Suspense fallback={<PageLoading />}>
            <Outlet key={businessId} />
          </Suspense>
          </RouteErrorBoundary>
        </main>
      </div>
      <BottomTabBar badges={navBadges} />

      {layer === "palette" ? <CommandPalette onClose={() => setLayer(null)} /> : null}
      {layer === "help" ? <ShortcutsHelp onClose={() => setLayer(null)} /> : null}
      {layer === "brake" ? (
        <BrakeDialog
          killSwitch={killSwitch.data}
          onClose={() => setLayer(null)}
          onEngage={async (input) => {
            await setKillSwitch.mutateAsync({ scope_kind: input.scope_kind, scope_id: input.scope_kind === "business" ? businessId : null, mode: input.mode, engaged: true, reason: input.reason });
            setLayer(null);
          }}
          onRelease={async (item, reason) => {
            await setKillSwitch.mutateAsync({
              scope_kind: item.scope_kind,
              scope_id: item.scope_id,
              mode: item.mode,
              engaged: false,
              reason: reason || "Reanudado desde el panel",
              typed_confirmation: "REACTIVAR",
            });
            setLayer(null);
          }}
        />
      ) : null}
    </div>
  );
}
