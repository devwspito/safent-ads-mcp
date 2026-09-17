import { Skeleton } from "./Skeleton";

/** Keep the frame visible while a route chunk or the initial session loads. */
export function PageLoading({ label = "Cargando vista…" }: { label?: string }) {
  return (
    <div role="status" aria-label={label} aria-busy="true" className="page-loading">
      <span className="visually-hidden">{label}</span>
      <Skeleton width="180px" height="24px" />
      <Skeleton height="64px" />
      <Skeleton height="160px" />
    </div>
  );
}
