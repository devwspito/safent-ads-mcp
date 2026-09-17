import { NavLink, useLocation } from "react-router-dom";
import type { Business, KillSwitchState } from "@/api/schemas";
import { MAIN_ROUTES } from "@/routes/routeConfig";
import type { NavBadge } from "@/utils/navBadges";
import { FreshnessBadge } from "./FreshnessBadge";
import { NavIcon } from "./NavIcon";
import styles from "./TopBar.module.css";

interface TopBarProps {
  businesses: Business[];
  businessId: string;
  onChangeBusiness: (id: string) => void;
  lagMinutes: number | undefined;
  noData?: boolean;
  killSwitch: KillSwitchState | undefined;
  onOpenBrake: () => void;
  onOpenHelp: () => void;
  badges: Record<string, NavBadge>;
}

/**
 * Barra superior única — panel-interaction-spec.md §1.1: negocio (solo si hay más de uno),
 * los cuatro destinos centrados en orden fijo, frescura y «Parar cambios» a la derecha.
 * Sin buscador visible: el mockup prescriptivo no lo lleva (§3.1); `Cmd/Ctrl+K` y la ayuda
 * de atajos («?») lo siguen abriendo, nunca hacen falta en pantalla (§1.1).
 * A ≤899 px cede los destinos a `BottomTabBar` y se reduce a negocio + estado.
 */
export function TopBar({ businesses, businessId, onChangeBusiness, lagMinutes, noData, killSwitch, onOpenBrake, onOpenHelp, badges }: TopBarProps) {
  const location = useLocation();
  const engaged = killSwitch?.effective.engaged ?? false;

  return (
    <header className={styles.topbar}>
      <div className={styles.leading}>
        {businesses.length > 1 ? (
          <>
            <label className="visually-hidden" htmlFor="business-switcher">
              Negocio
            </label>
            <select id="business-switcher" className={styles.select} value={businessId} onChange={(event) => onChangeBusiness(event.target.value)}>
              {businesses.map((business) => (
                <option key={business.business_id} value={business.business_id}>
                  {business.name}
                </option>
              ))}
            </select>
          </>
        ) : null}
      </div>

      <nav className={styles.nav} aria-label="Navegación principal">
        {MAIN_ROUTES.map((route) => {
          const badge = badges[route.path];
          return (
            <NavLink
              key={route.path}
              to={{ pathname: route.path, search: location.search }}
              className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navLinkActive : ""}`}
            >
              <NavIcon path={route.path} />
              <span>{route.label}</span>
              {badge?.count ? <span className={styles.count}>{badge.count}</span> : null}
              {badge?.dot ? <span className={`${styles.dot} ${badge.dot === "red" ? styles.dotRed : styles.dotAmber}`} aria-label={badge.dot === "red" ? "Necesita atención hoy" : "Necesita revisión"} /> : null}
            </NavLink>
          );
        })}
      </nav>

      <div className={styles.trailing}>
        <span className={styles.freshnessSlot}>
          <FreshnessBadge lagMinutes={lagMinutes} noData={noData} />
        </span>
        <button type="button" className={styles.helpButton} onClick={onOpenHelp} aria-label="Atajos de teclado">
          ?
        </button>
        <button
          type="button"
          className={`${styles.brakeButton} ${engaged ? styles.brakeButtonEngaged : ""}`}
          onClick={onOpenBrake}
          aria-pressed={killSwitch ? engaged : undefined}
          aria-label={!killSwitch ? "Parar cambios: estado sin verificar" : undefined}
        >
          {engaged ? "Cambios parados" : "Parar cambios"}
        </button>
      </div>
    </header>
  );
}
