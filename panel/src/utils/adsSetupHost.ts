import type { Platform } from "@/api/schemas";

/** UI capability only. The host also checks source, origin and current permission. */
export function hasAdsSetupHost(): boolean {
  try {
    return window.parent !== window &&
      window.parent.location.origin === window.location.origin &&
      window.frameElement?.getAttribute("data-safent-setup") === "true";
  } catch { return false; }
}

export function requestAdsSetup(provider: Platform): boolean {
  if ((provider !== "google" && provider !== "meta") || !hasAdsSetupHost()) return false;
  try {
    window.parent.postMessage({ type: "safent:ads-setup", provider }, window.location.origin);
    return true;
  } catch { return false; }
}
