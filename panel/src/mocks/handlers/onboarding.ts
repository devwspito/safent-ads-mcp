import { http, HttpResponse } from "msw";
import { getOnboardingStatus } from "../fixtures/onboarding";
import { API_BASE } from "./apiBase";

export const onboardingHandlers = [
  http.get(`${API_BASE}/onboarding`, () => HttpResponse.json(getOnboardingStatus())),
];
