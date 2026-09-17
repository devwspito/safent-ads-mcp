import { isSafeUrl } from "@/utils/url";
import type { PackagePreview } from "@/api/schemas/packages";
import styles from "./WhyBlock.module.css";

interface WhyBlockProps {
  why: PackagePreview["why"];
}

/**
 * `contracts/api.md` §7 punto 4: el encargo entrecomillado, el resumen y las fuentes como
 * enlaces. Sin fuentes externas ni internas: "Sin datos de competencia."
 */
export function WhyBlock({ why }: WhyBlockProps) {
  const hasResearch = Boolean(why.research && (why.research.internal.length > 0 || why.research.external.length > 0));

  return (
    <section aria-labelledby="package-why-heading" className={styles.wrap}>
      <h3 id="package-why-heading" className={styles.heading}>
        Por qué
      </h3>
      <p className={styles.request}>«{why.owner_request}»</p>
      <p className={styles.summary}>{why.summary}</p>
      <ul className={styles.criteria}>
        <li>{why.success_criterion}</li>
        <li>{why.kill_criterion}</li>
      </ul>

      {hasResearch && why.research ? (
        <div className={styles.research}>
          {why.research.internal.map((item, index) => (
            <p key={`internal-${index}`} className={styles.researchLine}>
              {item.summary}
            </p>
          ))}
          {why.research.external.map((item, index) =>
            item.url && isSafeUrl(item.url) ? (
              <a key={`external-${index}`} className={styles.researchLink} href={item.url} target="_blank" rel="noopener noreferrer">
                {item.summary}
              </a>
            ) : (
              <p key={`external-${index}`} className={styles.researchLine}>
                {item.summary}
              </p>
            ),
          )}
        </div>
      ) : (
        <p className={styles.noResearch}>Sin datos de competencia.</p>
      )}
    </section>
  );
}
