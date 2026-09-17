export function defaultGoogleClientType(origin: string): "web" | "desktop" {
  try {
    const url = new URL(origin);
    return ["http:", "https:"].includes(url.protocol) && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      ? "desktop" : "web";
  } catch { return "web"; }
}
