import type { ReactNode } from "react";
import { describeApiError } from "@/utils/apiError";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { Skeleton } from "./Skeleton";

interface QueryBoundaryProps<T> {
  isLoading: boolean;
  isError: boolean;
  /** Error real de la consulta (TanStack Query `.error`) — se traduce por clase con `describeApiError`. */
  error?: unknown;
  /** Sólo para casos que ya traducían el mensaje a mano; prefiere `error`. */
  errorMessage?: string;
  onRetry: () => void;
  data: T | undefined;
  isEmpty: (data: T) => boolean;
  emptyTitle: string;
  emptyBody?: string;
  emptyAction?: ReactNode;
  skeleton?: ReactNode;
  children: (data: T) => ReactNode;
}

/**
 * Orquesta cargando/error/vacío/datos para una consulta TanStack Query.
 * Recargar en segundo plano nunca pasa por aquí: placeholderData mantiene `data` poblado
 * y el llamador decide si añade un <ReloadingIndicator />.
 */
export function QueryBoundary<T>({
  isLoading,
  isError,
  error,
  errorMessage,
  onRetry,
  data,
  isEmpty,
  emptyTitle,
  emptyBody,
  emptyAction,
  skeleton,
  children,
}: QueryBoundaryProps<T>) {
  if (isLoading) {
    return <div role="status" aria-label="Cargando datos…" aria-busy="true">{skeleton ?? <Skeleton height="200px" />}</div>;
  }
  if (isError) {
    return <ErrorState message={errorMessage ?? describeApiError(error)} onRetry={onRetry} />;
  }
  if (data === undefined) {
    return <ErrorState message="No hay una respuesta verificable. Vuelve a consultar antes de continuar." onRetry={onRetry} />;
  }
  if (isEmpty(data)) {
    return <EmptyState title={emptyTitle} body={emptyBody} action={emptyAction} />;
  }
  return <>{children(data)}</>;
}
