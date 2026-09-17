import { describe, expect, it } from "vitest";
import { proposalDetailSchema } from "@/api/schemas/proposals";
import { googleSearchWithTargetingDetail } from "@/mocks/fixtures/googleSearchWithTargeting";
import { campaignCreationSchema, campaignPlanReady } from "./campaignCreation";

export const googlePlan = { schema_version: 1, platform: "google", name: "Campaña revisada", status: "PAUSED", daily_budget: { amount: "10.12", currency: "EUR" }, native: { advertising_channel_type: "SEARCH", bidding_strategy: "MANUAL_CPC", contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", network_settings: { target_google_search: true, target_search_network: false, target_content_network: false, target_partner_search_network: false } } };
const CONVERSION_GOAL = { resource_name: "customers/1234567890/conversionActions/987654321" };
describe("campaign creation wire plan", () => {
  it("preserves exact decimal text at the backend's 12-digit limit", () => {
    const plan = { ...googlePlan, daily_budget: { amount: "999999999999.99", currency: "EUR" } };
    expect(campaignCreationSchema.parse(plan).daily_budget.amount).toBe("999999999999.99");
  });
  it.each([10.12, "0", "-1", "1.001", "1e3", "NaN", "1000000000000.00", "90071992547409.99"])("rejects unsafe/rounded budget %s", amount => {
    expect(campaignCreationSchema.safeParse({ ...googlePlan, daily_budget: { amount, currency: "EUR" } }).success).toBe(false);
  });
  it("requires explicit politics, all networks and PAUSED without adding defaults", () => {
    const native = { ...googlePlan.native, contains_eu_political_advertising: undefined };
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native }).success).toBe(false);
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native: { ...googlePlan.native, network_settings: {} } }).success).toBe(false);
    expect(campaignCreationSchema.safeParse({ ...googlePlan, status: "ACTIVE" }).success).toBe(false);
  });
  it("requires category countries and a fresh server verdict, not merely a plausible object", () => {
    const plan = { ...googlePlan, platform: "meta", native: { objective: "OUTCOME_TRAFFIC", buying_type: "AUCTION", bid_strategy: "LOWEST_COST_WITHOUT_CAP", special_ad_categories: ["HOUSING"], special_ad_category_country: [] } };
    expect(campaignCreationSchema.safeParse(plan).success).toBe(false);
    expect(campaignPlanReady({ platform: "google", creation_plan: googlePlan, creation_plan_error: null })).toBe(true);
    expect(campaignPlanReady({ platform: "google", creation_plan: googlePlan })).toBe(false);
    expect(campaignPlanReady({ platform: "meta", creation_plan: googlePlan, creation_plan_error: null })).toBe(false);
  });
});

describe("google campaign creation — spec 005 channel types (regression: .strict() rejected the real server payload)", () => {
  it("parses the real SEARCH payload with geographic_targeting and marks the plan ready", () => {
    const detail = proposalDetailSchema.parse(googleSearchWithTargetingDetail());
    const parsed = campaignCreationSchema.parse(detail.creation_plan);
    if (parsed.platform !== "google" || parsed.native.advertising_channel_type !== "SEARCH") throw new Error("unexpected shape");
    expect(parsed.native.geographic_targeting).toEqual({ geo_target_constants: ["geoTargetConstants/2724"], positive_geo_target_type: "PRESENCE" });
    expect(campaignPlanReady(detail)).toBe(true);
  });

  it.each([
    ["DISPLAY", { advertising_channel_type: "DISPLAY", bidding_strategy: { kind: "MANUAL_CPC" }, contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING" }],
    ["DEMAND_GEN", { advertising_channel_type: "DEMAND_GEN", bidding_strategy: { kind: "MAXIMIZE_CONVERSIONS" }, contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", conversion_goals: [CONVERSION_GOAL] }],
    ["PERFORMANCE_MAX", { advertising_channel_type: "PERFORMANCE_MAX", bidding_strategy: { kind: "MAXIMIZE_CONVERSION_VALUE" }, contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", conversion_goals: [CONVERSION_GOAL] }],
  ] as const)("accepts the %s channel shape", (_label, native) => {
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native }).success).toBe(true);
  });

  it.each([
    { kind: "MANUAL_CPC" },
    { kind: "MAXIMIZE_CLICKS" },
    { kind: "MAXIMIZE_CLICKS", cpc_bid_ceiling: { amount: "2.50", currency: "EUR" } },
    { kind: "MAXIMIZE_CONVERSIONS" },
    { kind: "MAXIMIZE_CONVERSIONS", target_cpa: { amount: "5.00", currency: "EUR" } },
    { kind: "MAXIMIZE_CONVERSION_VALUE" },
    { kind: "MAXIMIZE_CONVERSION_VALUE", target_roas: "3.5" },
  ])("accepts DISPLAY with bidding_strategy shape $kind", (bidding) => {
    const native = { advertising_channel_type: "DISPLAY", bidding_strategy: bidding, contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING" };
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native }).success).toBe(true);
  });

  it("rejects DEMAND_GEN/PERFORMANCE_MAX without at least one conversion goal", () => {
    const native = { advertising_channel_type: "DEMAND_GEN", bidding_strategy: { kind: "MAXIMIZE_CONVERSIONS" }, contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING" };
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native }).success).toBe(false);
  });

  it("rejects MANUAL_CPC bidding on DEMAND_GEN (only conversion-based bidding is legal there)", () => {
    const native = { advertising_channel_type: "DEMAND_GEN", bidding_strategy: { kind: "MANUAL_CPC" }, contains_eu_political_advertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", conversion_goals: [CONVERSION_GOAL] };
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native }).success).toBe(false);
  });

  it("stays strict: an unrecognized key on the google native block is still rejected, never passed through", () => {
    const native = { ...googlePlan.native, unexpected_field: true };
    expect(campaignCreationSchema.safeParse({ ...googlePlan, native }).success).toBe(false);
  });
});
