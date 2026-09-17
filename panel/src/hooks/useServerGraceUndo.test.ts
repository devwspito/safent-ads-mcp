import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useServerGraceUndo } from "./useServerGraceUndo";

describe("useServerGraceUndo", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("expone la entrada mientras no llega el deadline servidor y la retira después", () => {
    const onUndo = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useServerGraceUndo({ onUndo }));

    act(() => {
      result.current.enqueue({ executionId: "exec_1", label: "Aprobado: Campaña X", deadline: Date.now() + 1_000 });
    });

    expect(result.current.entries).toHaveLength(1);
    expect(result.current.secondsLeft(result.current.entries[0]!)).toBeGreaterThan(0);

    act(() => {
      vi.advanceTimersByTime(1_300);
    });

    expect(result.current.entries).toHaveLength(0);
  });

  it("deshacer llama a onUndo con el execution_id y retira la entrada de inmediato", async () => {
    const onUndo = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useServerGraceUndo({ onUndo }));

    act(() => {
      result.current.enqueue({ executionId: "exec_2", label: "Aprobado: Campaña Y", deadline: Date.now() + 20_000 });
    });

    await act(async () => {
      await result.current.undo("exec_2");
    });

    expect(onUndo).toHaveBeenCalledWith("exec_2");
    expect(result.current.entries).toHaveLength(0);
  });

  it("deshacer todas usa el asidero de lote cuando está disponible", async () => {
    const onUndo = vi.fn().mockResolvedValue(undefined);
    const onUndoBatch = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useServerGraceUndo({ onUndo, onUndoBatch }));

    act(() => {
      result.current.enqueue({ executionId: "exec_3", label: "Aprobado: Campaña Z", deadline: Date.now() + 20_000 });
      result.current.enqueue({ executionId: "exec_4", label: "Aprobado: Campaña W", deadline: Date.now() + 20_000 });
    });

    await act(async () => {
      await result.current.undoAll();
    });

    expect(onUndoBatch).toHaveBeenCalledWith(["exec_3", "exec_4"]);
    expect(onUndo).not.toHaveBeenCalled();
    expect(result.current.entries).toHaveLength(0);
  });
});
