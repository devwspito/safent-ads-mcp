import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { packagePreviewSchema } from "./packages";

/**
 * `tests/contracts/package-preview-platform.json` lo genera y fija
 * `tests/integration/packages/test_package_preview_por_canal.py::
 * TestContratoDeCablePlataforma` desde el `panel_read._build_preview` real
 * (mismo patrón que `panelRead.contract.test.ts` / `cockpit-wire.json`) —
 * este archivo comprueba que el zod real del panel no descarta `account.name`
 * ni malinterpreta `publish_as` como parte de `account` (bug real: el
 * backend sólo declaraba `entity_ref`/`publish_as` anidado, commits
 * 998c92e/7172f7a).
 */
const wire = JSON.parse(
  readFileSync(resolve(process.cwd(), "../tests/contracts/package-preview-platform.json"), "utf8"),
);
const platformSchema = packagePreviewSchema.shape.platform;

describe("real backend `platform` block → panel contract", () => {
  it("parses the Google account without a Meta page, keeping the readable name", () => {
    // `publish_as` no está declarado en `packagePreviewSchema` todavía (el panel no lo pinta,
    // §Panel.md "Pinned"): el zod por defecto lo descarta sin fallar — sólo `account` viaja intacto.
    expect(platformSchema.parse(wire.google).account).toEqual(wire.google.account);
    expect(wire.google.account.name).toBe("100-000-0002");
    expect(wire.google.publish_as).toBeNull();
  });

  it("parses the Meta account with `publish_as` as a sibling of `account`, never nested inside it", () => {
    expect(platformSchema.parse(wire.meta).account).toEqual(wire.meta.account);
    expect(wire.meta.account).not.toHaveProperty("publish_as");
    expect(wire.meta.publish_as).toEqual({ page_name: "Clinica X" });
  });

  it("rejects an account without the required `name` — the real regression this pins", () => {
    const accountWithoutName: Record<string, unknown> = { ...wire.google.account };
    delete accountWithoutName.name;
    const malformed = { ...wire.google, account: accountWithoutName };
    expect(platformSchema.safeParse(malformed).success).toBe(false);
  });
});
