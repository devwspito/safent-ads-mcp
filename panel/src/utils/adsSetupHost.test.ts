import { afterEach, describe, expect, it, vi } from "vitest";
import type { Platform } from "@/api/schemas";
import { hasAdsSetupHost, requestAdsSetup } from "./adsSetupHost";

function embed(origin = window.location.origin, capability = "true") {
  const postMessage = vi.fn();
  const frame = document.createElement("iframe");
  frame.setAttribute("data-safent-setup", capability);
  vi.spyOn(window, "parent", "get").mockReturnValue({ location: { origin }, postMessage } as unknown as Window);
  vi.spyOn(window, "frameElement", "get").mockReturnValue(frame);
  return { frame, postMessage };
}

afterEach(() => vi.restoreAllMocks());

describe("Ads setup host", () => {
  it("no ofrece navegación fuera del iframe autorizado", () => {
    expect(hasAdsSetupHost()).toBe(false);
    expect(requestAdsSetup("meta")).toBe(false);
  });
  it.each(["meta", "google"] as const)("envía únicamente el proveedor %s al origen exacto", (provider) => {
    const { postMessage } = embed();
    expect(hasAdsSetupHost()).toBe(true);
    expect(requestAdsSetup(provider)).toBe(true);
    expect(postMessage).toHaveBeenCalledTimes(1);
    expect(postMessage).toHaveBeenCalledWith({ type: "safent:ads-setup", provider }, window.location.origin);
  });
  it("rechaza padres de otro origen", () => {
    const { postMessage } = embed("https://unrelated.example");
    expect(requestAdsSetup("meta")).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();
  });
  it.each(["", "false", "TRUE"])("no confía en una capacidad ausente o incorrecta: %s", (flag) => {
    const { postMessage } = embed(window.location.origin, flag);
    expect(hasAdsSetupHost()).toBe(false);
    expect(requestAdsSetup("google")).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();
  });
  it("vuelve a comprobar la capacidad al pulsar", () => {
    const { frame, postMessage } = embed();
    expect(hasAdsSetupHost()).toBe(true);
    frame.removeAttribute("data-safent-setup");
    expect(requestAdsSetup("meta")).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();
  });
  it("no envía proveedores arbitrarios", () => {
    const { postMessage } = embed();
    expect(requestAdsSetup("https://unrelated.example" as Platform)).toBe(false);
    expect(postMessage).not.toHaveBeenCalled();
  });
  it("controla los errores al acceder al padre o enviar sin filtrarlos", () => {
    vi.spyOn(window, "parent", "get").mockImplementation(() => { throw new Error("private"); });
    expect(hasAdsSetupHost()).toBe(false);
    expect(requestAdsSetup("meta")).toBe(false);
    vi.restoreAllMocks();
    const { postMessage } = embed();
    postMessage.mockImplementation(() => { throw new Error("private"); });
    expect(requestAdsSetup("meta")).toBe(false);
  });
});
