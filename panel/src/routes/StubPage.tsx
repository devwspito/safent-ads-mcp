import { EmptyState } from "@/components/states/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";

interface StubPageProps {
  title: string;
  body: string;
}

/**
 * Vista con la ruta, atajo y estado vacío correctos; la interacción llega en una
 * fase posterior (T084–T088, T110 de tasks.md). Copy provisional — pendiente de content-designer.
 */
export function StubPage({ title, body }: StubPageProps) {
  return (
    <div>
      <PageHeader title={title} />
      <EmptyState title="Todavía no hay nada que mostrar" body={body} />
    </div>
  );
}
