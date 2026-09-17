import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { expect, it, vi } from "vitest";
import { apiClient } from "@/api/client";
import { killSwitchStateSchema } from "@/api/schemas";
import { useSetKillSwitch } from "./killSwitch";

it("keeps the selected business in a global brake request", async () => {
  const post = vi.spyOn(apiClient, "post").mockResolvedValue({});
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useSetKillSwitch("business-1"), { wrapper });
  const input = { scope_kind: "global" as const, scope_id: null, mode: "ALL" as const, engaged: true, reason: "Pausa solicitada" };
  try {
    await act(async () => { await hook.result.current.mutateAsync(input); });
    expect(post).toHaveBeenCalledWith("/kill-switch", killSwitchStateSchema, input, { business_id: "business-1" });
  } finally {
    hook.unmount(); client.clear(); post.mockRestore();
  }
});
