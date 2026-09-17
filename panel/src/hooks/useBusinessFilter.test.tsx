import { renderHook, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, beforeEach } from "vitest";
import { useBusinessFilter } from "./useBusinessFilter";

const businesses = [{ business_id: "owned", name: "Mi negocio" }];
describe("business filter authority", () => {
  beforeEach(() => localStorage.clear());
  it("never returns a URL business until /me authorizes it", () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter initialEntries={["/?business_id=foreign"]}>{children}</MemoryRouter>;
    const { result, rerender } = renderHook(({ list }) => useBusinessFilter(list), { wrapper, initialProps: { list: undefined as typeof businesses | undefined } });
    expect(result.current.businessId).toBe("");
    rerender({ list: businesses });
    expect(result.current.businessId).toBe("owned");
    act(() => result.current.setBusinessId("foreign"));
    expect(result.current.businessId).toBe("owned");
    expect(localStorage.getItem("safent_business_id")).toBeNull();
    rerender({ list: [] });
    expect(result.current.businessId).toBe("");
  });
  it("honors an authorized URL and does not trust stale storage", () => {
    localStorage.setItem("safent_business_id", "foreign");
    const { result } = renderHook(() => useBusinessFilter(businesses), { wrapper: ({ children }) => <MemoryRouter initialEntries={["/?business_id=owned"]}>{children}</MemoryRouter> });
    expect(result.current.businessId).toBe("owned");
  });
});
