import type { Money } from "@/api/schemas";
import type { CockpitMoney } from "@/api/schemas/cockpit";

/** `34,20 €` bajo 100, `1.240 €` desde 100 — panel-visual-spec.md §2. */
export function formatMoney(money: Money): string {
  const hasDecimals = Math.abs(money.amount) < 100;
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: money.currency,
    minimumFractionDigits: hasDecimals ? 2 : 0,
    maximumFractionDigits: hasDecimals ? 2 : 0,
    useGrouping: true,
  }).format(money.amount);
}

/**
 * Misma regla visual que `formatMoney` para el `Money` del cockpit, cuyo `amount` viaja como
 * cadena decimal (cockpit-read-model.md §2) — aquí solo se formatea para pantalla, nunca se opera.
 */
export function formatCockpitMoney(money: CockpitMoney): string {
  const amount = Number(money.amount);
  const hasDecimals = Math.abs(amount) < 100;
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: money.currency,
    minimumFractionDigits: hasDecimals ? 2 : 0,
    maximumFractionDigits: hasDecimals ? 2 : 0,
    useGrouping: true,
  }).format(amount);
}

/** Equivalente mensual de un importe diario (×30) — panel-interaction-spec.md §3 ("la diferencia diaria y su equivalente mensual"). */
export function monthlyEquivalent(daily: Money): Money {
  return { amount: Math.round(daily.amount * 30 * 100) / 100, currency: daily.currency };
}

/** `+15 €`, `−12 €`, `Sin coste` — panel-interaction-spec.md §3.1 (impacto de una propuesta, siempre con signo). */
export function formatSignedMoney(money: Money): string {
  if (money.amount === 0) return "Sin coste";
  const hasDecimals = Math.abs(money.amount) < 100;
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: money.currency,
    minimumFractionDigits: hasDecimals ? 2 : 0,
    maximumFractionDigits: hasDecimals ? 2 : 0,
    signDisplay: "exceptZero",
    useGrouping: true,
  }).format(money.amount);
}

const percentFormatter = new Intl.NumberFormat("es-ES", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
  signDisplay: "exceptZero",
});

/** `+12,4 %` — con signo y un decimal siempre. */
export function formatPercent(value: number): string {
  return `${percentFormatter.format(value)} %`;
}

const numberFormatter = new Intl.NumberFormat("es-ES", { useGrouping: true });

export function formatNumber(value: number): string {
  return numberFormatter.format(value);
}

/**
 * Ritmo a fin de mes — design.md §5 ("Al ritmo actual, gastarás unos 940 € este mes"): el gasto
 * en lo que va de mes, extrapolado por los días transcurridos. Cifra real (`spend.mtd`), nunca
 * un porcentaje crudo; solo cambia cómo se proyecta, nunca se inventa el gasto ya hecho.
 */
export function projectedMonthEndSpend(mtd: Money, today: Date = new Date()): Money {
  const dayOfMonth = today.getDate();
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  const amount = dayOfMonth > 0 ? (mtd.amount / dayOfMonth) * daysInMonth : mtd.amount;
  return { amount: Math.round(amount * 100) / 100, currency: mtd.currency };
}
