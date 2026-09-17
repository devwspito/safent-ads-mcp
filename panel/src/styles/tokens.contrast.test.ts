import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guarda contra regresiones de contraste en `tokens.css` — panel-visual-spec.md §7
 * ("CI: test sobre los pares texto/fondo que falle bajo 4.5:1, y bajo 3:1 en `*-line`")
 * + prescripción de color 2026-09-16: cubre TODOS los tokens `*-line`/`*-text` del
 * archivo (no solo los tres marcadores de `StatusMarker`), contra los cuatro fondos
 * reales del panel — lienzo, superficie, elevado y barra lateral — porque muchos de
 * estos pares se usan como borde/texto sin relleno propio (p. ej. `rowCritical` en
 * `ProposalGroupCard.module.css` pinta `border-left-color` directamente sobre el
 * fondo de la tarjeta, nunca sobre su propio `-fill`).
 */
const TOKENS_PATH = join(process.cwd(), "src/styles/tokens.css");
const css = readFileSync(TOKENS_PATH, "utf-8");

function extractBlock(source: string, selector: string): string {
  const start = source.indexOf(selector);
  if (start === -1) throw new Error(`No se encontró el bloque "${selector}" en tokens.css`);
  const braceStart = source.indexOf("{", start);
  const braceEnd = source.indexOf("\n}", braceStart);
  return source.slice(braceStart, braceEnd);
}

/** Valor crudo de cada custom property del bloque: `#hex`, `var(--otro)` u otra cosa. */
function parseRawTokens(block: string): Record<string, string> {
  const tokens: Record<string, string> = {};
  const re = /--([a-z0-9-]+):\s*([^;]+);/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(block))) {
    tokens[match[1]!] = match[2]!.trim();
  }
  return tokens;
}

/** Sigue la cadena de `var(--x)` hasta el hex final; `null` si no resuelve a un color plano. */
function resolveHex(name: string, tokens: Record<string, string>, seen: Set<string> = new Set()): string | null {
  if (seen.has(name)) return null;
  seen.add(name);
  const raw = tokens[name];
  if (!raw) return null;
  const hexMatch = /^#[0-9a-fA-F]{6}$/.exec(raw);
  if (hexMatch) return raw.toLowerCase();
  const varMatch = /^var\(--([a-z0-9-]+)/.exec(raw);
  if (varMatch) return resolveHex(varMatch[1]!, tokens, seen);
  return null;
}

function linearize(channel: number): number {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b);
}

function contrastRatio(hexA: string, hexB: string): number {
  const lumA = luminance(hexA);
  const lumB = luminance(hexB);
  const [lighter, darker] = lumA > lumB ? [lumA, lumB] : [lumB, lumA];
  return (lighter + 0.05) / (darker + 0.05);
}

const rawLight = parseRawTokens(extractBlock(css, ":root {"));
const rawDark = parseRawTokens(extractBlock(css, '[data-theme="dark"] {'));

const BACKGROUND_NAMES = ["bg-canvas", "bg-surface", "bg-raised", "bg-sidebar"] as const;

const TEXT_MIN_RATIO = 4.5;
const NON_TEXT_MIN_RATIO = 3;

describe.each([
  ["claro", rawLight],
  ["oscuro", rawDark],
] as const)("contraste de tokens *-line/*-text (tokens.css, tema %s)", (_theme, tokens) => {
  const backgrounds = BACKGROUND_NAMES.map((name) => {
    const hex = resolveHex(name, tokens);
    if (!hex) throw new Error(`No se pudo resolver el fondo --${name} a un hex`);
    return { name, hex };
  });

  const textTokens = Object.keys(tokens).filter((key) => key.endsWith("-text") && resolveHex(key, tokens));
  const lineTokens = Object.keys(tokens).filter((key) => key.endsWith("-line") && resolveHex(key, tokens));

  it("encuentra al menos un token *-text y uno *-line que resuelvan a hex", () => {
    expect(textTokens.length).toBeGreaterThan(0);
    expect(lineTokens.length).toBeGreaterThan(0);
  });

  it.each(textTokens)(`%s cumple ${TEXT_MIN_RATIO}:1 sobre los cuatro fondos`, (name) => {
    const hex = resolveHex(name, tokens)!;
    for (const bg of backgrounds) {
      expect(contrastRatio(hex, bg.hex), `${name} vs --${bg.name}`).toBeGreaterThanOrEqual(TEXT_MIN_RATIO);
    }
  });

  it.each(lineTokens)(`%s cumple ${NON_TEXT_MIN_RATIO}:1 sobre los cuatro fondos`, (name) => {
    const hex = resolveHex(name, tokens)!;
    for (const bg of backgrounds) {
      expect(contrastRatio(hex, bg.hex), `${name} vs --${bg.name}`).toBeGreaterThanOrEqual(NON_TEXT_MIN_RATIO);
    }
  });
});
