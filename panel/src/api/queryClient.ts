import { QueryClient } from "@tanstack/react-query";
import { ApiRequestError } from "@/api/client";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        if (error instanceof ApiRequestError && error.status === 401) return false;
        return failureCount < 2;
      },
      staleTime: 30_000,
      // Medido en producción (16-sep, item 10): cada petición cuesta ~0,2-0,3 s y un cambio de
      // pestaña reactivaba TODO a la vez. Propuestas es la superficie de decisión — se le
      // devuelve `refetchOnWindowFocus: true` explícitamente en su propio hook.
      refetchOnWindowFocus: false,
    },
  },
});
