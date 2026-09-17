/**
 * Genera una vista previa SVG con el formato real (ancho×alto) mientras no hay activos
 * reales — mantiene la proporción para que "vista previa en formato real" (panel-interaction-spec.md
 * §3.5) sea cierto incluso con datos de ejemplo. Colores con nombre (no hex) para no romper
 * la regla de "hex sólo en tokens.css" del lint.
 */
export function placeholderCreativeUri(width: number, height: number, label: string): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <rect width="100%" height="100%" fill="gainsboro" />
    <rect x="1" y="1" width="${width - 2}" height="${height - 2}" fill="none" stroke="dimgray" stroke-width="2" />
    <text x="50%" y="50%" font-family="sans-serif" font-size="${Math.round(Math.min(width, height) * 0.06)}" fill="dimgray" text-anchor="middle" dominant-baseline="middle">${label}</text>
    <text x="50%" y="62%" font-family="sans-serif" font-size="${Math.round(Math.min(width, height) * 0.04)}" fill="darkslategray" text-anchor="middle">${width}×${height}</text>
  </svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
