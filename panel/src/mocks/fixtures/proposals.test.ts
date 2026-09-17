import { describe, expect, it } from "vitest";
import { listProposalGroups } from "./proposals";

describe("listProposalGroups — lente calendar_event", () => {
  it("agrupa por evt:<id> / evt:none y deja el grupo sin evento al final", () => {
    const response = listProposalGroups("calendar_event");

    expect(response.lens).toBe("calendar_event");
    expect(response.groups.length).toBeGreaterThan(1);
    for (const group of response.groups) {
      expect(group.cause_key).toMatch(/^evt:/);
      expect(group.group_kind).toBe("calendar_event");
    }

    const noEventIndex = response.groups.findIndex((group) => group.cause_key === "evt:none");
    expect(noEventIndex).toBeGreaterThan(-1);
    expect(noEventIndex).toBe(response.groups.length - 1);
    expect(response.groups[noEventIndex]!.cause).toBe("Sin evento de calendario");
  });

  it("las propuestas sin evento traen closes_at nulo", () => {
    const response = listProposalGroups("calendar_event");
    const noEventGroup = response.groups.find((group) => group.cause_key === "evt:none");
    expect(noEventGroup?.closes_at).toBeNull();
  });
});
