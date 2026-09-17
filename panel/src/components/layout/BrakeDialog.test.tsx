import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BrakeDialog } from "./BrakeDialog";

describe("emergency brake acknowledgment", () => {
  it("keeps the dialog open and blocks duplicate sends and Escape until acknowledged", async () => {
    let finish!: () => void;
    const onEngage = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<BrakeDialog killSwitch={undefined} onEngage={onEngage} onRelease={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", {name: "Parar cambios"}));
    fireEvent.click(screen.getByRole("button", {name: "Parando…"}));
    await user.keyboard("{Escape}");
    expect(onEngage).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-busy", "true");
    await act(async () => finish());
    expect(screen.getByRole("button", {name: "Parar cambios"})).toBeEnabled();
  });
  it("shows server failure in the dialog with retry, without dismissing", async () => {
    const onEngage = vi.fn().mockRejectedValue(new Error("network"));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<BrakeDialog killSwitch={undefined} onEngage={onEngage} onRelease={vi.fn()} onClose={onClose} />);
    await user.click(screen.getByRole("button", {name: "Parar cambios"}));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", {name: "Parar cambios"})).toBeEnabled();
  });
});
