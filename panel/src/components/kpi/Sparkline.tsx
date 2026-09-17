interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  ariaLabel: string;
}

/** Minigráfico 14 días — panel-visual-spec.md §5: tendencia, no valores; último punto marcado. */
export function Sparkline({ values, width = 64, height = 24, ariaLabel }: SparklineProps) {
  if (values.length === 0) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = width / (values.length - 1 || 1);

  const points = values.map((value, index) => {
    const x = index * stepX;
    const y = height - ((value - min) / range) * height;
    return [x, y] as const;
  });

  const path = points.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const lastPoint = points[points.length - 1] ?? [0, height];
  const [lastX, lastY] = lastPoint;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={ariaLabel}>
      <path d={path} fill="none" stroke="var(--chart-1)" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lastX} cy={lastY} r="2" fill="var(--chart-1)" />
    </svg>
  );
}
