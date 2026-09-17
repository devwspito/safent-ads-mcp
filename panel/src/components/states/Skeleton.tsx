import styles from "./Skeleton.module.css";

interface SkeletonProps {
  width?: string;
  height?: string;
  className?: string;
}

/** Bloque de carga con la forma real del contenido — panel-interaction-spec.md §2. */
export function Skeleton({ width = "100%", height = "1rem", className }: SkeletonProps) {
  return (
    <span
      className={[styles.skeleton, className].filter(Boolean).join(" ")}
      style={{ width, height, display: "inline-block" }}
      aria-hidden="true"
    />
  );
}
