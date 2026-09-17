/** Exportación CSV en cliente — sin librerías, escapado mínimo RFC 4180. */
export function toCsv(rows: Array<Record<string, string | number>>, columns: string[]): string {
  const escape = (value: string | number) => {
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const header = columns.join(",");
  const body = rows.map((row) => columns.map((col) => escape(row[col] ?? "")).join(","));
  return [header, ...body].join("\n");
}

export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
