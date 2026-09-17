/** Small local icon vocabulary; no icon-font request or extra UI dependency. */
export function NavIcon({ path }: { path: string }) {
  const paths: Record<string, string> = {
    "/propuestas": "M8 3h8v4H8z M8 5H5v16h14V5h-3 M8 13l3 3 5-6",
    "/campanas": "M4 20V4 M4 5h13l-3 5 3 5H4",
    "/resultados": "M4 20V10 M10 20V4 M16 20v-7 M4 20h16",
    "/ajustes": "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
  };
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[path] ?? paths["/propuestas"]} />
    </svg>
  );
}
