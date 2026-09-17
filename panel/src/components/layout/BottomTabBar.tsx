import { NavLink, useLocation } from "react-router-dom";
import { MAIN_ROUTES } from "@/routes/routeConfig";
import type { NavBadge } from "@/utils/navBadges";
import { NavIcon } from "./NavIcon";
import styles from "./BottomTabBar.module.css";

interface BottomTabBarProps {
  badges: Record<string, NavBadge>;
}

/** Barra inferior de cuatro pestañas a ≤899 px — panel-interaction-spec.md §1.1: mismo orden que la barra superior. */
export function BottomTabBar({ badges }: BottomTabBarProps) {
  const location = useLocation();

  return (
    <nav className={styles.bar} aria-label="Navegación inferior">
      {MAIN_ROUTES.map((route) => {
        const badge = badges[route.path];
        return (
          <NavLink
            key={route.path}
            to={{ pathname: route.path, search: location.search }}
            className={({ isActive }) => `${styles.tab} ${isActive ? styles.tabActive : ""}`}
          >
            <span className={styles.iconWrap}>
              <NavIcon path={route.path} />
              {badge?.count ? <span className={styles.count}>{badge.count}</span> : null}
              {badge?.dot ? <span className={`${styles.dot} ${badge.dot === "red" ? styles.dotRed : styles.dotAmber}`} aria-hidden="true" /> : null}
            </span>
            <span>{route.label}</span>
          </NavLink>
        );
      })}
    </nav>
  );
}
