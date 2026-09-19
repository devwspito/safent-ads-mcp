# Panel ↔ runtime: preparation jobs

The panel and MCP share a durable, business-scoped inbox. Approving a launch plan
commits the editorial review and one job for its exact content revision in the same
transaction. Reading a page does not start work. Older editorial approvals expose
an explicit **Iniciar preparación** action; no automatic approval backfill occurs.

This is a preparation workflow, **not an execution authorization**. A runtime result
creates/updates an actual `campaign_drafts` record through the existing validator.
It does not call providers, promote/approve proposals, publish videos, activate
campaigns, spend money, enable tracking or send WhatsApp messages. Native video,
lead optimization and local targeting limitations must be reported, not replaced
with unsupported approximations. Complete campaign packages remain a separate flow.

## Two connection modes

An active MCP client can call:

1. `list_runtime_jobs` / `get_runtime_job`: inspect work and approved context.
2. `claim_runtime_job`: claim one job with a private 120-second lease.
3. `heartbeat_runtime_job`: renew the lease and report progress every 20–30 seconds.
4. `propose_runtime_result`: submit a closed, validated result with a campaign draft
   and/or explicit blockers. Do not report success just because prose was generated.

Use the same business, runtime, instance ID and lease token for the whole attempt.
Never print the lease token. Coordination writes require proposal permission and,
for OAuth, `ads:propose`. Results remain proposal-class tools with native confirmation.
No tool accepts an approval, arbitrary command, callback URL or provider mutation.

For work to arrive **without a user typing into an active chat**, run the opt-in local
bridge. Ordinary MCP tool exposure does not promise to wake a closed Codex session.
The bridge polls the narrow HTTPS inbox every 30 seconds and invokes a bounded
non-interactive runtime process. It does not inject messages into an existing desktop
conversation. Claude channels are a separate integration, not implemented here.

## Connect a local runtime

Requirements: the engine repository and its `uv` environment, a supported local CLI
installed and signed in, and an authenticated owner session in the panel.

The normal installer registers the MCP and pairs this machine in one guided flow:

```sh
./scripts/instalar-mcp.sh --url https://ads.example.com/mcp --solo codex --background
```

Choose `--solo claude` for Claude Code. The panel opens an owner authorization page;
compare its device fingerprint with the terminal before authorizing. No token is
copied or placed in a URL. The server encrypts the narrow 30-day credential to an
ephemeral RSA-2048 public key; only the requesting machine can decrypt it. Delivery
expires after five minutes and requires a 256-bit verifier. Repeated identical
approval is idempotent. Revoking the connection invalidates pending delivery too.

The private 0600 profile is stored under `~/.local/state/safent-runtime/`. Reinstalling
reuses a valid profile and does not duplicate its user service. Each machine has a
separate authorization. Expiry/revocation stops the worker; reinstall to reauthorize.
Keep the repository and its Python environment at the same path while the service
is installed. An existing MCP name pointing elsewhere is never overwritten.

`--background` explicitly enables quota-consuming work using a macOS LaunchAgent or
Linux systemd user service (while the user session/machine is running). Without it,
pairing completes but no worker starts. Windows background services are not supported.
Use the printed profile path with these commands:

```sh
uv run python -m safent_ads.runtime.installation status --profile /absolute/path/profile.json
uv run python -m safent_ads.runtime.installation run --profile /absolute/path/profile.json
uv run python -m safent_ads.runtime.installation stop --profile /absolute/path/profile.json
```

`status` checks credential validity, not whether the process is running. `stop` removes
only the generated user service, keeping the private profile for reconnection. Revoke
the connection in the panel to invalidate its credential. Native `mcp add --url`
commands or the script's explicit `--solo-mcp` mode expose remote tools only: remote
MCP installation cannot silently install or launch a local worker.

### Advanced manual connection

In a launch detail, expand **Conector con Codex / Claude Code**, choose the runtime and
create a preparation access. Copy its one-time credential locally; never put it in
a chat, repository or command-line argument. It expires after 30 days and is revocable.
Only its SHA-256 digest is stored on the server. It grants this business's preparation
inbox access, not panel-owner rights, MCP-wide access or advertising credentials.

```sh
read -s SAFENT_RUNTIME_TOKEN
export SAFENT_RUNTIME_TOKEN
uv run python -m safent_ads.runtime.bridge \
  --url https://ads.example.com --runtime codex
```

Paste the credential at the hidden input prompt. Use `--runtime claude` for Claude
Code, `--executable /absolute/path/to/cli` if not on PATH, `--once` to process at most
one job and exit, and `--timeout 600` to bound each invocation (60–1800 seconds).
Closing the process stops receiving work. No launch agent/service is installed.
Continuous operation and model usage are opt-in; jobs consume the runtime's account
quota. No model is hard-coded. Runtime login remains local and is never copied to
the server. The bridge token is not inherited by the runtime process.

The built-in adapter takes the approved snapshot and existing draft context and
requests a schema-constrained result. Codex runs with ignored user configuration,
an ephemeral session and read-only sandbox in a temporary directory, with shell,
web search, apps, hooks and multi-agent tools explicitly disabled; Claude runs
without built-in tools or MCP servers. These adapters prepare structured data only;
they do not inherit the operator's broad MCP tools. Existing active MCP clients can
use the job tools above with their own normal permission checks instead.

Official CLI references checked for implementation:

- [Codex non-interactive execution](https://developers.openai.com/codex/noninteractive)
- [Codex MCP features and configuration](https://developers.openai.com/codex/mcp)
- [Claude Code headless operation](https://code.claude.com/docs/en/headless)

## Reliability and controls

- Business-scoped unique revision key prevents duplicate jobs from double clicks.
- `FOR UPDATE SKIP LOCKED`, hashed lease secrets and a holder identity fence workers.
- Approval and enqueue, and result and draft persistence, are atomic transactions.
- Repeated acknowledgements of the same result are idempotent.
- New approved revisions cancel unfinished old work. Changed files fence old results.
- Three expired leases fail visibly instead of creating an endless paid retry loop.
- Owners can cancel, revoke, or reply to blockers and retry explicitly from the panel.
- A retry uses the stable job-owned draft and its current optimistic revision; it
  cannot silently overwrite unrelated or subsequently edited drafts.
- No connection signal means disconnected, not “working”. An expired lease is shown
  as lost contact. Pending fields and activation requirements remain visible.
- Connections use bearer-only `/runtime/v1/*` endpoints, with no cookie fallback.
  Owner controls under `/api/v1/runtime/*` retain session, business and CSRF checks.

## Current scope

Supported job type: launch-plan preparation to one campaign draft. A `prepared`
result means a draft exists; it does **not** mean a landing is collecting leads or
that native campaign/ad-set/video entities have been created. The panel links the
real draft and separates missing preparation inputs from launch activation blockers.
Further job types must add typed contracts and verified artifacts; never generic
remote shell execution or “agent says done” completion.
