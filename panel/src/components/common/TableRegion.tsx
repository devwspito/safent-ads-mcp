import type { ReactNode } from "react";

/** Horizontal overflow belongs to the data, never to the whole workspace. */
export function TableRegion({ label, children }: { label: string; children: ReactNode }) {
  return <div className="table-region" role="region" aria-label={label} tabIndex={0}>{children}</div>;
}
