import { describe, expect, it } from "vitest";
import { defaultGoogleClientType } from "./googleClientDefault";

describe("Google local client default", () => {
  it.each(["http://127.0.0.1:43337", "http://localhost:9000", "http://[::1]:8000", "https://localhost"])("uses desktop PKCE for exact loopback %s", (origin) => {
    expect(defaultGoogleClientType(origin)).toBe("desktop");
  });
  it.each(["https://example.com", "https://localhost.example.com", "https://127.0.0.1.example.com", "https://example.com/?native=1", "tauri://localhost", "invalid"])("keeps web for %s", (origin) => {
    expect(defaultGoogleClientType(origin)).toBe("web");
  });
});
