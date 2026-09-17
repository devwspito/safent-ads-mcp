/**
 * Guarda de vocabulario genérico (vocabulary.md §7): Safent Ads es un producto genérico —
 * ningún fichero bajo panel/src debe mencionar vocabulario específico de un vertical
 * (educación, oposiciones, matrículas…). Ninguna referencia a terceros en el producto, ni
 * siquiera en listas de palabras prohibidas: esta guarda es solo de vocabulario vertical.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SELF_PATH = fileURLToPath(import.meta.url);
const SRC_ROOT = dirname(dirname(SELF_PATH));

const FORBIDDEN_PATTERNS: RegExp[] = [
  /convocatorias?/i,
  /oposici(?:ón|on|ones)/i,
  /matr[ií]culas?/i,
  /(?<!\ben )\bcursos?\b/i,
  /\bcourses?\b/i,
  /enrolments?/i,
  /academia/i,
  /temario/i,
  /especialidad/i,
  /denominacion/i,
  /fecha_(?:inicio|fin)_plazo/i,
  /fecha_examen/i,
  /ventana_abierta/i,
  /\bformaci[oó]n\b/i,
  /\bex[aá]men(?:es)?\b/i,
  /\balumn[oa]s?\b/i,
  /\bopositor(?:es|a|as)?\b/i,
  /\bprofesor(?:es|a|as)?\b/i,
  /\bdocentes?\b/i,
  /\bacad[eé]mic[oa]s?\b/i,
];

function collectFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = join(dir, entry.name);
    return entry.isDirectory() ? collectFiles(fullPath) : [fullPath];
  });
}

function matchedPatterns(content: string): string[] {
  return FORBIDDEN_PATTERNS.filter((pattern) => pattern.test(content)).map((pattern) => pattern.source);
}

describe("vocabulario vertical fuera de panel/src", () => {
  it("ningún fichero bajo panel/src menciona vocabulario de un vertical", () => {
    const offenders = collectFiles(SRC_ROOT)
      .filter((path) => path !== SELF_PATH)
      .map((path) => ({ path, hits: matchedPatterns(readFileSync(path, "utf-8")) }))
      .filter((result) => result.hits.length > 0);

    expect(offenders).toEqual([]);
  });
});
