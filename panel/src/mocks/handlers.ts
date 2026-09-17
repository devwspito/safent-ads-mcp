import { authHandlers } from "./handlers/auth";
import { badgesHandlers } from "./handlers/badges";
import { brandHandlers } from "./handlers/brand";
import { cockpitHandlers } from "./handlers/cockpit";
import { cloudflareHandlers } from "./handlers/cloudflare";
import { connectionsHandlers } from "./handlers/connections";
import { creativesHandlers } from "./handlers/creatives";
import { decisionLogHandlers } from "./handlers/decisionLog";
import { economicsHandlers } from "./handlers/economics";
import { entityLifecycleHandlers } from "./handlers/entityLifecycle";
import { executionsHandlers } from "./handlers/executions";
import { federatedLoginHandlers } from "./handlers/federatedLogin";
import { hardCapsHandlers } from "./handlers/hardCaps";
import { killSwitchHandlers } from "./handlers/killSwitch";
import { mcpOauthHandlers } from "./handlers/mcpOauth";
import { onboardingHandlers } from "./handlers/onboarding";
import { packagesHandlers } from "./handlers/packages";
import { platformAppsHandlers } from "./handlers/platformApps";
import { portfolioSignalsHandlers } from "./handlers/portfolioSignals";
import { proposalsHandlers } from "./handlers/proposals";
import { rulesHandlers } from "./handlers/rules";
import { settingsHandlers } from "./handlers/settings";

export const handlers = [
  ...authHandlers,
  ...cockpitHandlers,
  ...portfolioSignalsHandlers,
  ...killSwitchHandlers,
  ...proposalsHandlers,
  ...packagesHandlers,
  ...executionsHandlers,
  ...entityLifecycleHandlers,
  ...rulesHandlers,
  ...decisionLogHandlers,
  ...creativesHandlers,
  ...connectionsHandlers,
  ...settingsHandlers,
  ...badgesHandlers,
  ...brandHandlers,
  ...platformAppsHandlers,
  ...economicsHandlers,
  ...onboardingHandlers,
  ...mcpOauthHandlers,
  ...cloudflareHandlers,
  ...federatedLoginHandlers,
  ...hardCapsHandlers,
];

export { setMockFreshIdentificationUntil, setMockSessionForTests, setMockSessionOrigin } from "./handlers/auth";
export { resetCockpitFixtures } from "./fixtures/cockpit";
export { resetBrandFixtures, setBrandKitFixture } from "./handlers/brand";
export { resetPlatformAppsFixtures } from "./fixtures/platformApps";
export { resetEconomicsFixtures } from "./handlers/economics";
export { resetRulesFixtures } from "./fixtures/rules";
export { resetConnectionsFixtures } from "./fixtures/connections";
export { resetOnboardingFixtures, setOnboardingStep } from "./fixtures/onboarding";
export { MOCK_CONSENT_TXN_ID, markMockFederatedPresenceFresh, resetMcpOauthFixtures } from "./fixtures/mcpOauth";
export { resetCloudflareFixtures } from "./fixtures/cloudflare";
export { setMockFederatedLoginAvailable } from "./handlers/federatedLogin";
export { resetHardCapsFixtures, setMockEnvelopeDeclared, setMockPanelStateAvailable } from "./fixtures/hardCaps";
