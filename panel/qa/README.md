# Campaign creation browser / real PostgreSQL check

Developer-only harness, **not a product entry point**. The regular panel build does not include this HTML entry. Its TypeScript refuses production mode. Run only in a disposable review checkout, never a deployed checkout.

## Isolation

- `campaign_creation_server.py` creates a new `postgres:16-alpine` testcontainer with default fictitious test credentials. It accepts **no database URL, provider URL, credentials or target-host arguments**. Existing databases are not opened or migrated. The testcontainer is disposed when the process exits its context.
- API bind is fixed to `127.0.0.1:5211`. There is no execution worker, broker or provider process. Approving a test proposal only adds a durable test authorization/queue entry; it does not deliver ads.
- The backend reuses repository test factories, the actual Container, session authentication, CSRF middleware and proposal/read/execution routes. No auth dependency override.
- Vite binds only `127.0.0.1:5212` and proxies only to the loopback harness. No arbitrary target URL is accepted. Both the page and browser runner check the isolated-server marker before operating. Do not proxy those ports to production.
- Synthetic cookie strings are intentionally public QA fixtures. They exist only in the freshly created database. No login, OAuth, keychain or saved browser profile is accessed.
- Browser runs a new headless Chrome context and closes it on success or failure. Only screenshots and synthetic request/result metadata are written to `QA_OUTPUT_DIR`.

## Prerequisites

Install this repository's Python development dependencies (including Testcontainers/Alembic), frontend dependencies with `npm ci`, an installed Chrome browser, and Playwright in a **separate temporary tools directory** or an existing trusted developer tool installation. No Playwright package is added to the product bundle.

Use an available Docker-compatible daemon. On Linux rootless Podman:

```sh
export DOCKER_HOST=unix:///run/user/1000/podman/podman.sock
export TESTCONTAINERS_RYUK_DISABLED=true
```

Keep port 5211 on the backend host and 5212 on the browser host unused. The backend has an intentionally short synthetic session; start a fresh harness for every complete browser run (it approves one fixture and changes its state).

## Run

1. From the repository root, with the repository dev environment activated:

   ```sh
   PYTHONPATH=src:. python panel/qa/campaign_creation_server.py
   ```

   Wait for the JSON `{"qa":"ready", ...}` line. A PostgreSQL container is created and migrated from scratch. No existing `.env` file is loaded by the test settings factory.

2. If the backend is on a separate review host, forward the **loopback-only QA port** from a separate terminal:

   ```sh
   ssh -N -o ExitOnForwardFailure=yes -L 5211:127.0.0.1:5211 YOUR_REVIEW_HOST
   ```

3. From `panel`, launch the dedicated QA entry. The paths are absolute on purpose:
   this directory is `panel/qa`, not the one at the repository root, and a bare
   relative path is ambiguous to a reader and to the export guard alike.


   ```sh
   NODE_OPTIONS=--no-experimental-webstorage npm run dev -- --config "$PWD/qa/vite.config.ts"
   ```

4. Set the absolute local path to the installed Playwright module and a fresh output directory, then run from `panel`:

   ```sh
   export QA_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs
   export QA_OUTPUT_DIR=/absolute/path/to/isolated-qa-results
   node "$PWD/qa/campaign-creation-browser.mjs"
   ```

   The module path must be absolute; the browser target is deliberately not configurable away from loopback. The runner returns a nonzero exit on a failed assertion and writes `result.json` only after every assertion passes. Screenshots may exist from an incomplete run: do not count those as success.

5. Stop the backend normally with Ctrl+C and let the testcontainer context exit, then stop Vite and the optional SSH tunnel. No server should remain after QA. If the host/process was forcibly killed, identify and remove **only that harness's testcontainer**, not other containers or databases.

## Assertions

Missing native plan blocks buttons and approval shortcuts. A second authenticated client changes the plan while the browser reviews it; stale PATCH gets a real 409 and preserves the draft. Explicit reload → edit → save succeeds without adding an approval. A no-op preserves its hash and closes normally. Only separate explicit approval, with the new hash, increments authorization and execution counts. Workers never execute them.

The runner also measures enabled label/body/input/action contrast from computed colors in light/dark schemes (≥4.5:1), keyboard access to the 390 px review CTA after native scrolling settles, absence of horizontal overflow and JavaScript errors. This tests the Google creation path; Meta-specific UI branches have separate schema/DOM/fixture visual tests. It does not verify Google/Meta account eligibility or create real resources.
