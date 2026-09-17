import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { proposalDetailSchema, proposalsResponseSchema } from "./proposals";

const contract = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/contracts/proposal-panel.json"), "utf8"));

describe("persisted Python/TypeScript proposals contract", () => {
  it("accepts the actual inbox without dropping required approval data", () => {
    expect(proposalsResponseSchema.parse(contract.inbox)).toEqual(contract.inbox);
  });
  it("accepts actual evidence and explicit unavailable analytics", () => {
    expect(proposalDetailSchema.parse(contract.detail)).toEqual(contract.detail);
  });
});
