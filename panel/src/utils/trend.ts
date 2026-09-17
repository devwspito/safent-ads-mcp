import type { PortfolioWindow } from "@/api/queries/portfolio";

/** Suma día a día las series de 14 puntos de varias filas — nunca inventa un punto que falte. */
export function aggregateSeries(series: number[][]): number[] {
  if (series.length === 0) return [];
  const length = series[0]!.length;
  return Array.from({ length }, (_, day) => series.reduce((sum, points) => sum + (points[day] ?? 0), 0));
}

export interface SeriesTrend {
  sparkline: number[];
  /** `null` a 30 días: la serie de 14 puntos no alcanza para comparar (design.md §5.2). */
  deltaPct: number | null;
}

/**
 * La serie de 14 días parte por la mitad para comparar (design.md §5.2): últimos 7 puntos
 * frente a los 7 anteriores. A 30 días no hay dato suficiente — nunca se inventa una diferencia.
 */
export function seriesTrend(points: number[], window: PortfolioWindow): SeriesTrend {
  if (window === "30D") return { sparkline: points, deltaPct: null };
  const half = Math.floor(points.length / 2);
  const previous = points.slice(0, half).reduce((sum, v) => sum + v, 0);
  const current = points.slice(half).reduce((sum, v) => sum + v, 0);
  const deltaPct = previous > 0 ? ((current - previous) / previous) * 100 : null;
  return { sparkline: points, deltaPct };
}
