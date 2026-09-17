/**
 * Réplica exacta de `brand/domain/discovery.contrast_ratio_on_white` (luminancia relativa WCAG):
 * el borrador de marca no trae el contraste medido para cada candidato de color (solo lo trae
 * `ColorSwatch`, que exige el valor ya calculado); el panel lo calcula aquí con la misma fórmula
 * para poder mostrarlo mientras el propietario revisa candidatos, antes de confirmar la paleta.
 */
const LINEAR_THRESHOLD = 0.03928;
const LINEAR_DIVISOR = 12.92;
const GAMMA_OFFSET = 0.055;
const GAMMA_EXPONENT = 2.4;
const WCAG_AA_NORMAL_TEXT_THRESHOLD = 4.5;

function linearizeChannel(channel: number): number {
  if (channel <= LINEAR_THRESHOLD) return channel / LINEAR_DIVISOR;
  return ((channel + GAMMA_OFFSET) / (1 + GAMMA_OFFSET)) ** GAMMA_EXPONENT;
}

function relativeLuminance(r: number, g: number, b: number): number {
  return 0.2126 * linearizeChannel(r) + 0.7152 * linearizeChannel(g) + 0.0722 * linearizeChannel(b);
}

export function contrastRatioOnWhite(hex: string): number {
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const luminance = relativeLuminance(r, g, b);
  return Math.round(((1.0 + 0.05) / (luminance + 0.05)) * 100) / 100;
}

export function meetsWcagAaNormalText(contrastRatio: number): boolean {
  return contrastRatio >= WCAG_AA_NORMAL_TEXT_THRESHOLD;
}
