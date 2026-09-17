import styles from "./CampaignStatusTabs.module.css";

export type CampaignStatusFilter = "activas" | "pausadas" | "todas";

interface CampaignStatusTabsProps {
  value: CampaignStatusFilter;
  onChange: (value: CampaignStatusFilter) => void;
  counts: Record<CampaignStatusFilter, number>;
  listId: string;
}

const TABS: Array<{ value: CampaignStatusFilter; label: string }> = [
  { value: "activas", label: "Activas" },
  { value: "pausadas", label: "Pausadas" },
  { value: "todas", label: "Todas" },
];

/** Segmented tabs — reemplazan el texto fijo "N activas" (panel-interaction-spec.md). */
export function CampaignStatusTabs({ value, onChange, counts, listId }: CampaignStatusTabsProps) {
  function handleKeyDown(event: React.KeyboardEvent, index: number) {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const nextIndex = event.key === "ArrowRight" ? (index + 1) % TABS.length : (index - 1 + TABS.length) % TABS.length;
    const next = TABS[nextIndex]!;
    onChange(next.value);
    (document.getElementById(`campanas-tab-${next.value}`) as HTMLButtonElement | null)?.focus();
  }

  return (
    <div className={styles.tablist} role="tablist" aria-label="Filtrar campañas por estado">
      {TABS.map((tab, index) => {
        const selected = tab.value === value;
        return (
          <button
            key={tab.value}
            id={`campanas-tab-${tab.value}`}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={listId}
            tabIndex={selected ? 0 : -1}
            className={`${styles.tab} ${selected ? styles.tabActive : ""}`}
            onClick={() => onChange(tab.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {tab.label} <span className={styles.count}>{counts[tab.value]}</span>
          </button>
        );
      })}
    </div>
  );
}
