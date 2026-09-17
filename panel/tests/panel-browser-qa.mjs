// Run against Vite's EXPLICIT existing MSW demo, never against live ad accounts.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const { chromium } = await import(pathToFileURL(process.env.SAFENT_PLAYWRIGHT_MODULE).href);
const output = process.env.SAFENT_QA_OUTPUT;
assert(output);
await mkdir(output, { recursive: true });
const base = 'http://127.0.0.1:18888';
const routes = [
  ['/cockpit', 'Cuadro de mando'], ['/campanas', 'Campañas'], ['/senales', 'Señales'],
  ['/propuestas', 'Propuestas'], ['/creatividades', 'Creatividades'], ['/reglas', 'Reglas'],
  ['/registro', 'Registro'], ['/conexiones', 'Conexiones'], ['/ajustes', 'Ajustes'],
];
const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: 'dark', reducedMotion: 'reduce' });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${base}/login`);
    await page.getByLabel('Correo', { exact: true }).fill('owner@negocio-ejemplo.es');
    await page.getByLabel('Contraseña', { exact: true }).fill('demo1234');
    await page.getByRole('button', { name: 'Continuar', exact: true }).click();
    await page.getByRole('heading', { name: 'Cartera', exact: true }).waitFor();
    // Screenshots must not be mistaken for real campaign/account results.
    await page.evaluate(() => { const note = document.createElement('div'); note.textContent = 'QA · FIXTURES MSW · SIN CUENTAS REALES'; note.style.cssText = 'position:fixed;bottom:2px;right:8px;z-index:9999;background:#202020;color:#ddd;padding:2px 6px;font:10px system-ui;pointer-events:none'; document.body.append(note); });
    async function capture(label) {
      await page.waitForTimeout(200);
      const sizes = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, main: document.querySelector('main').clientWidth, mainScroll: document.querySelector('main').scrollWidth }));
      assert(sizes.document <= sizes.viewport, `${width} ${label} document overflow ${JSON.stringify(sizes)}`);
      assert(sizes.mainScroll <= sizes.main + 1, `${width} ${label} main overflow ${JSON.stringify(sizes)}`);
      await page.screenshot({ path: `${output}/${width}-${label}.png` });
    }
    async function go(label) {
      if (width === 390) await page.getByRole('button', { name: 'Expandir navegación', exact: true }).click();
      await page.getByRole('navigation', { name: 'Navegación principal', exact: true }).getByRole('link', { name: label, exact: false }).click();
      await page.getByRole('heading', { name: label, exact: true }).waitFor();
    }
    await capture('cartera');
    for (const [path, label] of routes) {
      await go(label);
      await capture(path.slice(1));
      if (path === '/propuestas') {
        const detail = page.getByRole('button', { name: 'Detalle', exact: true });
        if (await detail.count()) { await detail.first().click(); await page.getByText('Límites de seguridad', { exact: true }).first().waitFor(); await capture('propuesta-detalle'); }
      }
    }
    await page.keyboard.press('Control+k');
    await page.getByRole('dialog', { name: /buscar y saltar/i }).waitFor();
    await page.waitForFunction(() => getComputedStyle(document.querySelector('[role="dialog"]')).opacity === '1');
    await page.screenshot({ path: `${output}/${width}-palette.png` });
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'Freno', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await page.waitForFunction(() => getComputedStyle(document.querySelector('[role="dialog"]')).opacity === '1');
    assert(await page.getByRole('dialog').evaluate(dialog => dialog.contains(document.activeElement)));
    await page.screenshot({ path: `${output}/${width}-brake-review.png` });
    await page.keyboard.press('Escape');
    // Loading/error/empty from fixture transport; never dispatch approval writes.
    for (const mode of ['loading', 'error', 'empty']) {
      await page.evaluate(async mode => { const qa = await import('/tests/panel-qa-controls.ts'); await qa.cockpitState(mode); }, mode);
      await go('Cuadro de mando');
      if (mode === 'loading') await page.getByRole('status', { name: 'Cargando datos…' }).waitFor();
      if (mode === 'error') await page.getByRole('alert').filter({ hasText: 'No se ha podido cargar' }).waitFor({ timeout: 10_000 });
      if (mode === 'empty') await page.getByText('Sin cuentas conectadas', { exact: true }).waitFor();
      await capture(`state-${mode}`);
      await go('Campañas');
    }
    assert.deepEqual(errors, []);
    console.log(`${width}px: 10 routes, detail, palette, brake review, loading/error/empty, no horizontal overflow/page errors PASS (MSW fixtures)`);
    await context.close();
  }
} finally { await browser.close(); }
