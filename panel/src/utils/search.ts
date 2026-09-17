/** Comparación de texto sin acentos ni mayúsculas — buscar campaña por nombre (design.md §4.4). */
export function normalizeSearch(value: string): string {
  return value.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
}

export function matchesSearch(haystack: string, query: string): boolean {
  const normalizedQuery = normalizeSearch(query.trim());
  if (!normalizedQuery) return true;
  return normalizeSearch(haystack).includes(normalizedQuery);
}
