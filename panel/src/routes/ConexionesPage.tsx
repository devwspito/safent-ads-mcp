import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMe } from "@/api/queries/auth";
import { usePlatformAccounts } from "@/api/queries/connections";
import { useOnboardingStatus } from "@/api/queries/onboarding";
import { useDeletePlatformAppCredentials, usePlatformApps } from "@/api/queries/platformApps";
import type { Platform } from "@/api/schemas";
import { AccountCard } from "@/components/connections/AccountCard";
import { CloudflareConnectionCard } from "@/components/connections/CloudflareConnectionCard";
import { ConnectedAgentsSection } from "@/components/connections/ConnectedAgentsSection";
import { ConnectProviderCard } from "@/components/connections/ConnectProviderCard";
import { DeveloperCredentialsCard } from "@/components/connections/DeveloperCredentialsCard";
import { MetaSystemUserTokenForm } from "@/components/connections/MetaSystemUserTokenForm";
import { OnboardingProgress } from "@/components/connections/OnboardingProgress";
import { QueryBoundary } from "@/components/states/QueryBoundary";
import { ErrorState } from "@/components/states/ErrorState";
import { describeApiError } from "@/utils/apiError";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";
import { hasAdsSetupHost, requestAdsSetup } from "@/utils/adsSetupHost";
import styles from "./ConexionesPage.module.css";

const PROVIDERS: Platform[] = ["google", "meta"];

export function ConexionesPage() {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  const accountsQuery = usePlatformAccounts(businessId);
  const platformAppsQuery = usePlatformApps();
  const onboardingQuery = useOnboardingStatus();
  const deletePlatformApp = useDeletePlatformAppCredentials();
  const [params] = useSearchParams();
  const [setupError, setSetupError] = useState<string | null>(null);
  const technicalDetails = useRef<HTMLDetailsElement>(null);
  const providerGrid = useRef<HTMLDivElement>(null);
  const guidedSetup = hasAdsSetupHost();
  const selectedProvider = params.get("provider");

  useEffect(() => {
    if (selectedProvider !== "google" && selectedProvider !== "meta") return;
    // Returning from setup selects the provider, never starts OAuth automatically.
    providerGrid.current?.querySelector<HTMLElement>(`[data-provider="${selectedProvider}"]`)?.focus();
  }, [selectedProvider]);

  function openSetup(provider: Platform) {
    setSetupError(null);
    if (guidedSetup) {
      if (!requestAdsSetup(provider)) setSetupError("No pudimos abrir la guía. Vuelve a Safent y entra en Capacidades → Integraciones para configurar la conexión.");
      return;
    }
    // Standalone deployments keep their existing direct-API settings.
    if (technicalDetails.current) {
      technicalDetails.current.open = true;
      technicalDetails.current.querySelector<HTMLElement>(`[data-setup-provider="${provider}"]`)?.focus();
    }
  }

  function statusOf(provider: Platform) {
    return platformAppsQuery.data?.items.find((item) => item.platform === provider);
  }

  return (
    <div>
      <h2 className={styles.groupTitle}>Tus cuentas</h2>

      {platformAppsQuery.isError ? <ErrorState message={describeApiError(platformAppsQuery.error)} onRetry={() => void platformAppsQuery.refetch()} /> : null}
      {deletePlatformApp.isError ? <p role="alert">{describeApiError(deletePlatformApp.error)}</p> : null}
      {setupError ? <p role="alert">{setupError}</p> : null}

      <section className={styles.section} aria-labelledby="connect-accounts-title">
        <h3 id="connect-accounts-title" className={styles.sectionTitle}>Conectar tus cuentas</h3>
        <p className={styles.explanation}>Inicia sesión en Google o Meta y autoriza el acceso a tus cuentas publicitarias. Tu contraseña se introduce allí, no en Safent.</p>
        <div className={styles.providerGrid} ref={providerGrid}>
        {PROVIDERS.map((provider) => (
          <div key={provider} className={styles.provider} data-provider={provider} tabIndex={-1}>
          <ConnectProviderCard
            provider={provider}
            ownerId={me?.owner_id ?? ""}
            businessId={businessId}
            googleCustomerIdRequired={provider === "google" && statusOf(provider)?.configured === true && statusOf(provider)?.client_id_masked === null}
            configured={!platformAppsQuery.isError && !platformAppsQuery.isFetching && (statusOf(provider)?.configured ?? false)}
            configurationPending={platformAppsQuery.isFetching}
            configurationError={platformAppsQuery.isError}
            guidedSetup={guidedSetup}
            onSetup={() => openSetup(provider)}
          />
          </div>
        ))}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="integrations-title">
        <h3 id="integrations-title" className={styles.sectionTitle}>Integraciones</h3>
        <p className={styles.explanation}>Herramientas adicionales, independientes de tus cuentas de anuncios.</p>
        <CloudflareConnectionCard />
      </section>

      {!guidedSetup ? (
        <details className={styles.advanced} ref={technicalDetails}>
          <summary>Ver configuración técnica</summary>
          <h3 className={styles.sectionTitle}>Preparar la conexión · Administración</h3>
          <p className={styles.explanation}>{platformAppsQuery.isError
            ? "No hemos podido comprobar la preparación de la conexión. Reintenta la comprobación antes de continuar."
            : platformAppsQuery.isFetching
              ? "Comprobando la preparación de las conexiones…"
              : PROVIDERS.every(provider => statusOf(provider)?.configured)
                ? "La configuración de las aplicaciones está guardada. Para conectar una cuenta, aún debes autorizar el acceso en Google o Meta."
                : "Elige «Configurar paso a paso» en la plataforma que necesitas. Primero guardas los datos de su aplicación y después autorizas tu cuenta."}</p>
          <p>En esta versión, quien administra Safent configura un cliente o aplicación propia una vez por instalación. Son datos de la aplicación, no tu correo ni tu contraseña. Guardarlos no conecta cuentas ni confirma los permisos del proveedor.</p>
          <OnboardingProgress status={onboardingQuery.data} isLoading={onboardingQuery.isLoading} />
          {onboardingQuery.isError ? <ErrorState message="No se ha podido comprobar el progreso de la configuración." onRetry={() => void onboardingQuery.refetch()} /> : null}
          <fieldset className={styles.providerGrid} disabled={platformAppsQuery.isError || platformAppsQuery.isFetching}>
          {PROVIDERS.map((provider) => (
            <div key={provider} data-setup-provider={provider} tabIndex={-1}>
            <DeveloperCredentialsCard
              platform={provider}
              status={statusOf(provider)}
              isLoading={platformAppsQuery.isLoading}
              onDelete={(platform) => deletePlatformApp.mutate(platform)}
              isDeleting={deletePlatformApp.isPending}
            />
            </div>
          ))}
          </fieldset>
        </details>
      ) : null}

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>Cuentas publicitarias</h3>
        <QueryBoundary
          isLoading={accountsQuery.isLoading}
          isError={accountsQuery.isError}
          error={accountsQuery.error}
          onRetry={() => void accountsQuery.refetch()}
          data={accountsQuery.data}
          isEmpty={(data) => data.items.length === 0}
          emptyTitle="Sin cuentas conectadas"
          emptyBody="Conecta una cuenta de Google Ads o Meta Ads para empezar."
        >
          {(data) => (
            <div className={styles.accountGrid}>
              {data.items.map((account) => (
                <AccountCard key={account.platform_account_id} account={account} businessId={businessId} />
              ))}
            </div>
          )}
        </QueryBoundary>
      </section>

      {!guidedSetup ? <details className={styles.advanced}>
        <summary>Otra forma de conectar Meta</summary>
        <p>Para conexiones de servidor mediante un usuario del sistema. No es el inicio de sesión OAuth ni sustituye la configuración de la aplicación.</p>
        <MetaSystemUserTokenForm businessId={businessId} />
      </details> : null}

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Aplicaciones con acceso</h2>
        <ConnectedAgentsSection />
      </section>
    </div>
  );
}
