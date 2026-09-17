import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { portfolioResponseSchema, type PortfolioResponse } from "@/api/schemas";

export type PortfolioWindow = "7D" | "14D" | "30D";

export function usePortfolio(businessId: string, window: PortfolioWindow) {
  return useQuery<PortfolioResponse>({
    queryKey: ["portfolio", businessId, window],
    queryFn: () => apiClient.get("/portfolio", portfolioResponseSchema, { business_id: businessId, window }),
    enabled: Boolean(businessId),
    refetchInterval: 60_000,
    placeholderData: (previous) => previous,
  });
}
