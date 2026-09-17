import { useMemo, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { Modal } from "@/components/common/Modal";
import { ALL_ROUTES } from "@/routes/routeConfig";
import styles from "./CommandPalette.module.css";

interface CommandPaletteProps {
  onClose: () => void;
}

/** `Cmd/Ctrl+K`: salto de ruta — panel-interaction-spec.md §1. */
export function CommandPalette({ onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const navigate = useNavigate();
  const location = useLocation();

  const matches = useMemo(
    () => ALL_ROUTES.filter((route) => normalize(route.label).includes(normalize(query.trim()))),
    [query],
  );

  function go(path: string) {
    navigate({ pathname: path, search: location.search });
    onClose();
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((i) => Math.max(0, Math.min(i + 1, matches.length - 1)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter" && matches[activeIndex]) {
      event.preventDefault();
      go(matches[activeIndex].path);
    }
  }

  return (
    <Modal label="Buscar y saltar a una vista" onClose={onClose} width="min(560px, 92vw)">
      <input
        autoFocus
        className={styles.input}
        placeholder="Ir a…"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setActiveIndex(0);
        }}
        onKeyDown={handleKeyDown}
        role="combobox"
        aria-label="Buscar una vista"
        aria-autocomplete="list"
        aria-expanded="true"
        aria-controls="command-palette-list"
        aria-activedescendant={matches[activeIndex] ? `cmd-${matches[activeIndex].path}` : undefined}
      />
        <ul className={styles.list} id="command-palette-list" role="listbox">
          {matches.map((route, index) => (
            <li
              key={route.path}
              id={`cmd-${route.path}`}
              role="option"
              aria-selected={index === activeIndex}
              className={`${styles.item} ${index === activeIndex ? styles.itemActive : ""}`}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => go(route.path)}
            >
              <span>{route.label}</span>
              {route.shortcut ? <span>{route.shortcut}</span> : null}
            </li>
          ))}
        </ul>
      {matches.length === 0 ? <p className={styles.empty} role="status">Sin resultados. Prueba con otro nombre.</p> : null}
    </Modal>
  );
}

function normalize(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}
