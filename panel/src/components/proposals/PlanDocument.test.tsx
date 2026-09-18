import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PlanDocument } from "./PlanDocument";

describe("Plan documents", () => {
  it("renders headings, lists and tables as readable content", () => {
    render(<PlanDocument text={"# Plan\n\n- First\n- Second\n\n| Stage | Budget |\n|---|---|\n| First | 15 € |"} />);
    expect(screen.getByRole("heading", { name: "Plan" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "15 €" })).toBeInTheDocument();
  });
  it("escapes raw HTML and only makes HTTP links clickable", () => {
    const { container } = render(<PlanDocument text={'<script>alert(1)</script>\n[Bad](javascript:alert(1))\n[Source](https://example.com)'} />);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.queryByRole("link", { name: "Bad" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Source" })).toHaveAttribute("href", "https://example.com");
  });
});
