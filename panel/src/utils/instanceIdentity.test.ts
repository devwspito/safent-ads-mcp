import { afterEach, describe, expect, it } from "vitest";
import { instanceIdentity } from "./instanceIdentity";

afterEach(() => {
  document.querySelectorAll('meta[name="ads-instance-name"]').forEach(el => el.remove());
  document.querySelectorAll('meta[name="ads-panel-host"]').forEach(el => el.remove());
});

function metadata(name: string, content: string) {
  const meta = document.createElement("meta");
  meta.name = name;
  meta.content = content;
  document.head.append(meta);
}

describe("instanceIdentity", () => {
  it("defaults to a generic name and the current host when nothing was injected", () => {
    expect(instanceIdentity()).toEqual({ name: "Ads MCP", panelHost: location.host });
  });

  it("reads the name and host that the server injected as meta tags", () => {
    metadata("ads-instance-name", "Acme Ads MCP");
    metadata("ads-panel-host", "ads.acme.example");

    expect(instanceIdentity()).toEqual({ name: "Acme Ads MCP", panelHost: "ads.acme.example" });
  });

  it("falls back to the default name when only the host was injected", () => {
    metadata("ads-panel-host", "ads.acme.example");

    expect(instanceIdentity()).toEqual({ name: "Ads MCP", panelHost: "ads.acme.example" });
  });

  it("ignores ambiguous metadata (more than one tag with the same name)", () => {
    metadata("ads-instance-name", "Acme Ads MCP");
    metadata("ads-instance-name", "Other Ads MCP");

    expect(instanceIdentity().name).toBe("Ads MCP");
  });
});
