import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { PageLoading } from "@/components/states/PageLoading";
import { LoginPage } from "@/routes/LoginPage";
import { getAdsBasePath } from "@/utils/basePath";

const PropuestasPage = lazy(() => import("@/routes/PropuestasPage").then((m) => ({ default: m.PropuestasPage })));
const WorkspacesPage = lazy(() => import("@/routes/WorkspacesPage").then(m => ({ default: m.WorkspacesPage })));
const WorkspacePage = lazy(() => import("@/routes/WorkspacesPage").then(m => ({ default: m.WorkspacePage })));
const LaunchProposalPage = lazy(() => import("@/routes/LaunchProposalPage").then((m) => ({ default: m.LaunchProposalPage })));
const RuntimePairingPage = lazy(() => import("@/routes/RuntimePairingPage").then((m) => ({ default: m.RuntimePairingPage })));
const PackagePreviewIndexPage = lazy(() => import("@/routes/PackagePreviewPage").then((m) => ({ default: m.PackagePreviewIndexPage })));
const PackagePreviewPage = lazy(() => import("@/routes/PackagePreviewPage").then((m) => ({ default: m.PackagePreviewPage })));
const CampanasPage = lazy(() => import("@/routes/CampanasPage").then((m) => ({ default: m.CampanasPage })));
const CarteraPage = lazy(() => import("@/routes/CarteraPage").then((m) => ({ default: m.CarteraPage })));
const AjustesPage = lazy(() => import("@/routes/AjustesPage").then((m) => ({ default: m.AjustesPage })));
const AjustesNegocioPage = lazy(() => import("@/routes/AjustesNegocioPage").then((m) => ({ default: m.AjustesNegocioPage })));
const ReglasPage = lazy(() => import("@/routes/ReglasPage").then((m) => ({ default: m.ReglasPage })));
const RegistroPage = lazy(() => import("@/routes/RegistroPage").then((m) => ({ default: m.RegistroPage })));
// Spec 002 (mcp_oauth): pantalla de consentimiento del AS, fuera de
// `AppShell` -- la abre el navegador desde `/authorize`, no la navegacion
// del panel. `ConexionesPage` ya no se carga aqui: `/conexiones` redirige a
// `/ajustes` (panel-interaction-spec.md SS1.2).
const ConsentimientoPage = lazy(() =>
  import("@/routes/ConsentimientoPage").then((m) => ({ default: m.ConsentimientoPage })),
);

/**
 * Cuatro destinos — panel-interaction-spec.md §1: Propuestas · Campañas · Resultados ·
 * Ajustes. El aterrizaje es siempre Propuestas (decisión 2: "es el trabajo diario, decidir").
 * Las ocho rutas anteriores se pliegan aquí; las que no tienen destino propio redirigen
 * (§1.2) para que ningún enlace ni marcador antiguo se rompa.
 */
export function App() {
  return (
    <BrowserRouter basename={getAdsBasePath()}>
      <Suspense fallback={<PageLoading />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/oauth/autorizar" element={<ConsentimientoPage />} />
          <Route path="/runtime/vincular" element={<RuntimePairingPage />} />
          <Route element={<AppShell />}>
            <Route path="/trabajo" element={<WorkspacesPage />} />
            <Route path="/trabajo/:id" element={<WorkspacePage />} />
            <Route path="/propuestas" element={<PropuestasPage />} />
            <Route path="/propuestas/lanzamiento/:slug" element={<LaunchProposalPage />} />
            <Route path="/propuestas/historial" element={<RegistroPage />} />
            {/* Ruta de comprobación T050/T051 (tasks.md) — pendiente de que T052 la enganche en la fila. */}
            <Route path="/propuestas/paquete" element={<PackagePreviewIndexPage />} />
            <Route path="/propuestas/paquete/:packageId" element={<PackagePreviewPage />} />
            <Route path="/campanas" element={<CampanasPage />} />
            <Route path="/resultados" element={<CarteraPage />} />
            <Route path="/ajustes" element={<AjustesPage />} />
            <Route path="/ajustes/negocio" element={<AjustesNegocioPage />} />
            <Route path="/ajustes/avanzado" element={<ReglasPage />} />
          </Route>

          {/* Rutas retiradas — panel-interaction-spec.md §1.2 */}
          <Route path="/cartera" element={<Navigate to="/resultados" replace />} />
          <Route path="/cockpit" element={<Navigate to="/resultados" replace />} />
          <Route path="/conexiones" element={<Navigate to="/ajustes" replace />} />
          <Route path="/reglas" element={<Navigate to="/ajustes" replace />} />
          <Route path="/senales" element={<Navigate to="/propuestas" replace />} />
          <Route path="/creatividades" element={<Navigate to="/campanas" replace />} />
          <Route path="/registro" element={<Navigate to="/propuestas/historial" replace />} />

          <Route path="/" element={<Navigate to="/trabajo" replace />} />
          <Route path="*" element={<Navigate to="/propuestas" replace />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
