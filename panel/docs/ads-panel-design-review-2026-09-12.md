# Ads panel: workspace and interaction review — 2026-09-12

## Scope and authority

This batch improves the existing Ads panel across Cartera, Cuadro de mando, Campañas, Señales, Propuestas, Creatividades, Reglas, Registro, Conexiones and Ajustes. It is a cohesive shared-shell/component redesign, not a claim that every component was rewritten or every provider operation is certified.

Emil's design-engineering skill informed density, useful hierarchy, restrained motion, predictable keyboard focus, and truthful empty/error states. No backend contracts, approval authority, account scope, credentials, campaign mutations or provider integrations were changed. No real ad accounts were operated. Existing production data comes from the same query layer; browser QA below explicitly uses the existing MSW demonstration.

## Before | After | Why

| Before | After | Why |
| --- | --- | --- |
| Wide static navigation and a long mobile navigation strip | Collapsible desktop icon rail; compact two-column mobile menu that closes after navigation | Give the work area space without hiding accessible route names or business scope |
| Equal visual priority for every cockpit KPI | Three priority metrics, then six smaller metrics on desktop; responsive two-column mobile layout | Make the first scan useful and bring actual account information into view sooner |
| Repeated large boxed change items | Compact, keyboard-scrollable chronological strip | Reduce visual competition without deleting evidence |
| Dense features without consistent introductory context | Shared page heading guidance for each route | Distinguish signals, proposals, execution records and configuration |
| Large proposal rows and creative previews | Compact proposal rows and contained 16:10 creative previews | Improve scan density while preserving full creative content and approval details |
| Missing query data could appear as an empty result | Missing response produces an explicit retryable verification error; only verified empty data yields an empty state | Never imply that zero accounts or zero work was confirmed when no response exists |
| Dialog focus included hidden/disabled descendants; busy state needed regression coverage | Focus ignores hidden and disabled-fieldset inputs, traps inside the dialog and restores a connected opener; busy Escape/backdrop remains blocked | Predictable keyboard use without weakening pending actions |
| Uncoordinated motion and browser-default brake fieldsets | Restrained dialog entry, explicit reduced-motion behavior, paused hidden-page animations, compact brake controls | Feedback with no animation requirement for keyboard/reduced-motion users |
| Existing mutation cleanup ESLint warning | Capture the stable ref object, not its numeric generation | Remove the warning while still invalidating the latest in-flight generation on unmount |

## Verification

- **123 passed**, 15 focused test files: shell/navigation, modal, query boundary, App, route accessibility, contrast tokens and all existing route test files. This includes 12 axe route cases and 12 contrast checks. Log: `/tmp/safent-ads-panel-focused.log`.
- After the final brake styling and stable-ref cleanup change: **15 passed**, `useConfirmedMutation`, `BrakeDialog`, `Modal`. This is an overlapping focused regression run, not 15 additional unique cases.
- `npm run lint`: **passed with zero warnings**, including the pre-existing cleanup warning now fixed without changing mutation semantics.
- `npm run build`: **passed**. Final log: `/tmp/safent-ads-panel-build-final.log`.
- `git diff --check -- panel`: passed before report creation; repeated at handoff.
- No full repository/backend suite was run for this frontend batch.

Focused command, from `panel`:

```sh
NODE_OPTIONS=--no-experimental-webstorage npm test -- src/components/layout/Sidebar.test.tsx src/components/common/Modal.test.tsx src/components/states/QueryBoundary.test.tsx src/App.test.tsx src/__tests__/a11yRoutes.test.tsx src/styles/tokens.contrast.test.ts src/routes/CockpitPage.test.tsx src/routes/PropuestasPage.test.tsx src/routes/CreatividadesPage.test.tsx src/routes/CarteraPage.test.tsx src/routes/SenalesPage.test.tsx src/routes/ConexionesPage.test.tsx src/routes/ReglasPage.test.tsx src/routes/RegistroPage.test.tsx src/routes/AjustesPage.test.tsx
NODE_OPTIONS=--no-experimental-webstorage npm test -- src/hooks/useConfirmedMutation.test.ts src/components/layout/BrakeDialog.test.tsx src/components/common/Modal.test.tsx
npm run lint
NODE_OPTIONS=--no-experimental-webstorage npm run build
```

## Browser evidence

Own ephemeral headless Chrome contexts, **1280×900 and 390×900**, dark theme and `prefers-reduced-motion: reduce`. Both final runs passed: ten routes, proposal detail, keyboard command palette, brake review, loading/error/verified-empty states, zero document/main horizontal overflow and zero page errors. Tables retain their own horizontal scrolling. Dialog focus and mobile menu closure were checked. Screenshots were manually inspected for density, hierarchy, mobile layouts, dialog rendering and error presentation.

Evidence: `/tmp/safent-ads-panel-visual.SHz7Xw/` (32 screenshots). Every screenshot is watermarked **QA · FIXTURES MSW · SIN CUENTAS REALES**. The last brake review was inspected after the final fieldset/radio styling. No brake was activated and no proposal was approved by the browser harness.

Reproduce using the existing Vite MSW demo, with no live credentials:

```sh
NODE_OPTIONS=--no-experimental-webstorage npm run dev -- --host 127.0.0.1 --port 18888 --strictPort
SAFENT_PLAYWRIGHT_MODULE=<ruta absoluta a playwright/index.mjs> SAFENT_QA_OUTPUT=<directorio de salida> node tests/panel-browser-qa.mjs
```

`tests/panel-qa-controls.ts` is imported only by the manual test harness through Vite. It changes fixture transport states, not product behavior. The browser check is **not live API/provider certification**, not proof that Meta/Google authorization is configured, and not backend HTTP end-to-end evidence. Normal-motion timing and additional browsers/light theme are not separately certified in this batch.

## Exact integration paths

Modified:

```text
panel/src/components/cockpit/ChangeStripBanner.module.css
panel/src/components/cockpit/ChangeStripBanner.tsx
panel/src/components/common/Modal.module.css
panel/src/components/common/Modal.test.tsx
panel/src/components/common/Modal.tsx
panel/src/components/creatives/CreativeCard.module.css
panel/src/components/kpi/KpiTile.module.css
panel/src/components/layout/AppShell.module.css
panel/src/components/layout/AppShell.tsx
panel/src/components/layout/BrakeDialog.module.css
panel/src/components/layout/PageHeader.module.css
panel/src/components/layout/PageHeader.tsx
panel/src/components/layout/Sidebar.module.css
panel/src/components/layout/Sidebar.tsx
panel/src/components/layout/TopBar.module.css
panel/src/components/layout/TopBar.tsx
panel/src/components/proposals/ProposalGroupCard.module.css
panel/src/components/states/EmptyState.module.css
panel/src/components/states/ErrorState.module.css
panel/src/components/states/QueryBoundary.tsx
panel/src/hooks/useConfirmedMutation.ts
panel/src/routes/CockpitPage.module.css
panel/src/routes/CreatividadesPage.module.css
panel/src/styles/global.css
```

New:

```text
panel/src/components/layout/NavIcon.tsx
panel/src/components/layout/Sidebar.test.tsx
panel/src/components/states/QueryBoundary.test.tsx
panel/tests/panel-browser-qa.mjs
panel/tests/panel-qa-controls.ts
panel/docs/ads-panel-design-review-2026-09-12.md
```

Do not include pre-existing `.ui-*` fixtures, generated `dist`, screenshots, or concurrent backend edits in this UI batch. No dependencies were added. The test-only tools are not imported into the production entry point. The existing account authorization, kill-switch scope, stale proposal detection and confirmation mutation flow remain authoritative.
