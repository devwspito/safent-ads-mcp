import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "@/api/client";
import { useConfirmedMutation } from "./useConfirmedMutation";

const challenge = () => new ApiRequestError("Revisar", "CONFIRMATION_REQUIRED", 428, {
  confirmation_token: "fixture-proof", expires_at: new Date(Date.now() + 120_000).toISOString(),
});
const summarize = (v: { label: string }) => [v.label];

describe("explicit owner confirmation", () => {
  it("prepares without proof then waits; freezes displayed and sent snapshot", async () => {
    const mutate = vi.fn().mockRejectedValueOnce(challenge()).mockResolvedValue({ ok: true });
    const onSuccess = vi.fn();
    const { result } = renderHook(() => useConfirmedMutation<{ label: string }, unknown>({ scopeKey: "a", mutate, summarize, onSuccess }));
    const variables = { label: "Original" };
    act(() => result.current.start(variables));
    variables.label = "Changed";
    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    expect(result.current.summary).toEqual(["Original"]);
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenLastCalledWith({ label: "Original" });
    act(() => { result.current.confirm(); result.current.confirm(); });
    await waitFor(() => expect(result.current.isPromptOpen).toBe(false));
    expect(mutate).toHaveBeenCalledTimes(2);
    expect(mutate).toHaveBeenLastCalledWith({ label: "Original", confirmationToken: "fixture-proof" });
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it("does not repeat the write or fetch another proof after a timeout", async () => {
    const mutate = vi.fn().mockRejectedValueOnce(challenge()).mockRejectedValue(new Error("sensitive backend details"));
    const { result } = renderHook(() => useConfirmedMutation<{ label: string }, unknown>({ scopeKey: "a", mutate, summarize }));
    act(() => result.current.start({ label: "x" }));
    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.errorMessage).toMatch(/Comprueba el estado/));
    act(() => result.current.confirm());
    expect(mutate).toHaveBeenCalledTimes(2);
    expect(result.current.canConfirm).toBe(false);
    expect(result.current.errorMessage).not.toContain("sensitive");
  });

  it("cancels without sending proof and never auto-confirms", async () => {
    const mutate = vi.fn().mockRejectedValue(challenge());
    const { result } = renderHook(() => useConfirmedMutation<{ label: string }, unknown>({ scopeKey: "a", mutate, summarize }));
    act(() => { result.current.start({ label: "x" }); result.current.start({ label: "y" }); });
    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    act(() => result.current.cancel());
    act(() => result.current.confirm());
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(result.current.isPromptOpen).toBe(false);
  });

  it("discards pending proof on business change including late preparation", async () => {
    let reject!: (error: unknown) => void;
    const mutate = vi.fn().mockImplementation(() => new Promise((_resolve, rejectPromise) => { reject = rejectPromise; }));
    const { result, rerender } = renderHook(({ scopeKey }) => useConfirmedMutation<{ label: string }, unknown>({ scopeKey, mutate, summarize }), { initialProps: { scopeKey: "a" } });
    act(() => result.current.start({ label: "x" }));
    rerender({ scopeKey: "b" });
    await act(async () => reject(challenge()));
    act(() => result.current.confirm());
    expect(result.current.isPromptOpen).toBe(false);
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it.each([new Error("leaky"), new ApiRequestError("no", "CONFIRMATION_REQUIRED", 428, {}), new ApiRequestError("no", "CONFIRMATION_REQUIRED", 428, { confirmation_token: "t", expires_at: "2000-01-01T00:00:00Z" })])("fails closed on invalid preparation", async (error) => {
    const mutate = vi.fn().mockRejectedValue(error);
    const { result } = renderHook(() => useConfirmedMutation<{ label: string }, unknown>({ scopeKey: "a", mutate, summarize }));
    act(() => result.current.start({ label: "x" }));
    await waitFor(() => expect(result.current.errorMessage).not.toBeNull());
    act(() => result.current.confirm());
    expect(result.current.canConfirm).toBe(false);
    expect(mutate).toHaveBeenCalledTimes(1);
  });
});
