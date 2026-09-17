import type { ReactNode } from "react";
import styles from "./PageHeader.module.css";

interface PageHeaderProps {
  title: string;
  actions?: ReactNode;
}

const CONTEXT: Record<string, string> = {
  'Resultados': 'Gasto y resultado por plataforma y cuenta, en el periodo elegido.',
  'Campañas': 'Revisa la estructura y prepara cambios con aprobación.',
  'Propuestas': 'Decide qué aprobar: qué cambia, cuánto dinero y por qué.',
  'Historial': 'Qué hizo el agente y qué decidiste tú.',
  'Ajustes': 'Preferencias de este espacio publicitario.',
  'Tu negocio': 'Marca, ofertas y economía, calendario y conversiones de este negocio.',
};

export function PageHeader({ title, actions }: PageHeaderProps) {
  return (
    <div className={styles.header}>
      <div className={styles.heading}><h1 className={styles.title}>{title}</h1>{CONTEXT[title] && <p className={styles.description}>{CONTEXT[title]}</p>}</div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
    </div>
  );
}
