/** PRNG determinista (mulberry32) para que las fixtures sean estables entre ejecuciones de test. */
export function hashSeed(seed: string): number {
  let h = 1779033703 ^ seed.length;
  for (let i = 0; i < seed.length; i++) {
    h = Math.imul(h ^ seed.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  return h >>> 0;
}

export function makeRng(seed: string): () => number {
  let a = hashSeed(seed);
  return function rng() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function series(seed: string, length: number, base: number, spreadPct: number): number[] {
  const rng = makeRng(seed);
  const out: number[] = [];
  for (let i = 0; i < length; i++) {
    const wobble = 1 + (rng() * 2 - 1) * spreadPct;
    out.push(Math.round(base * wobble * 100) / 100);
  }
  return out;
}

/** Elige un elemento de una lista no vacía usando un valor [0,1) — nunca produce `undefined`. */
export function pickFrom<T>(items: readonly T[], unitRandom: number): T {
  const index = Math.min(items.length - 1, Math.floor(unitRandom * items.length));
  const item = items[index];
  if (item === undefined) throw new Error("pickFrom: lista vacía en una fixture");
  return item;
}
