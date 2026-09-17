import { afterEach, describe, expect, it } from "vitest";
import { getAdsBasePath, isAdsEmbedded } from "./basePath";

afterEach(() => {
  document.querySelectorAll('meta[name="safent-ads-base-path"]').forEach(el => el.remove());
});

function metadata(content: string) {
  const meta = document.createElement("meta");
  meta.name = "safent-ads-base-path";
  meta.content = content;
  document.head.append(meta);
}

describe("getAdsBasePath", () => {
  it("es cadena vacía cuando el documento no inyectó nada (directo)", () => {
    expect(getAdsBasePath()).toBe("");
  });

  it("lee el prefijo que composition/app.py inyectó cuando está empotrado", () => {
    metadata("/ads");

    expect(getAdsBasePath()).toBe("/ads");
  });
});

describe("isAdsEmbedded", () => {
  it("es false por defecto (servido en directo)", () => {
    expect(isAdsEmbedded()).toBe(false);
  });

  it("es true cuando el documento se sirvió bajo el prefijo empotrado", () => {
    metadata("/ads");

    expect(isAdsEmbedded()).toBe(true);
  });
});

it.each(["", "https://evil.example", "//evil.example", "/ads/", "/arbitrary"])("rejects unreviewed prefix %s", value => {
  metadata(value);
  expect(getAdsBasePath()).toBe("");
  expect(isAdsEmbedded()).toBe(false);
});

it("rejects ambiguous metadata", () => {
  metadata("/ads"); metadata("");
  expect(getAdsBasePath()).toBe("");
});
