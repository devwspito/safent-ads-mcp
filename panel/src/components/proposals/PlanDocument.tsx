import type { ReactNode } from "react";
import styles from "./LaunchPlans.module.css";

/** Small review-document renderer: text is always React-escaped; no HTML execution. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)]+\))/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    return part;
  });
}

export function PlanDocument({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index] ?? "";
    if (!line.trim()) continue;
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      blocks.push((heading[1] ?? "").length <= 2 ? <h3 key={index}>{inline(heading[2] ?? "")}</h3> : <h4 key={index}>{inline(heading[2] ?? "")}</h4>);
    } else if (line.startsWith("|")) {
      const rows: string[][] = [];
      const start = index;
      while (index < lines.length && (lines[index] ?? "").startsWith("|")) {
        const cells = (lines[index] ?? "").replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
        if (!cells.every(cell => /^:?-+:?$/.test(cell))) rows.push(cells);
        index++;
      }
      index--;
      blocks.push(<div key={start} className={styles.tableScroll}><table><thead><tr>{rows[0]?.map((cell, i) => <th key={i}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.slice(1).map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell)}</td>)}</tr>)}</tbody></table></div>);
    } else if (/^[-*] /.test(line) || /^\d+\. /.test(line)) {
      const numbered = /^\d+\. /.test(line);
      const pattern = numbered ? /^\d+\. / : /^[-*] /;
      const items: ReactNode[] = [];
      const start = index;
      while (index < lines.length && pattern.test(lines[index] ?? "")) {
        items.push(<li key={index}>{inline((lines[index] ?? "").replace(pattern, ""))}</li>);
        index++;
      }
      index--;
      blocks.push(numbered ? <ol key={start}>{items}</ol> : <ul key={start}>{items}</ul>);
    } else {
      blocks.push(<p key={index}>{inline(line)}</p>);
    }
  }
  return <>{blocks}</>;
}
