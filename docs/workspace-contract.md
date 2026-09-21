# Shared workspace contract v1

The server, not a chat, owns marketing project state. The REST panel and MCP
adapters call `workspaces.presentation.command` and the same `WorkspaceStore`.
Clients discover `contract_version: 1`; no client-specific business logic exists.

## Commands

| MCP | HTTP | Result |
| --- | --- | --- |
| list_workspaces | GET /api/v1/workspaces | Scoped durable projects |
| get_workspace | GET /api/v1/workspaces/{id} | Brief, drafts, decisions, execution receipts, resources, activity, runtime jobs |
| propose_workspace | POST /api/v1/workspaces | Create or PATCH context using stable workspace_key and expected_revision |
| propose_workspace_campaign | POST /api/v1/workspaces/{id}/campaigns | Save an existing/new linked draft; never create a remote campaign |
| propose_workspace_creation | POST /api/v1/workspaces/{id}/prepare-campaign | Idempotently prepare a PAUSED campaign proposal for human review |

Omitted fields remain unchanged, explicit null clears a nullable field. The SDK
wrapper preserves unset fields through both validation passes. `resources` replaces
the complete list and requires the current workspace revision. The revision is a
concurrency fence, not an approval. HTTP writes require the owner session and CSRF;
MCP writes require normal business-scoped PROPOSE permission and quotas. No new
approval path or native-provider credential is exposed.

## Lifecycle and evidence

1. Context and materials are durable planning data, not authorizations.
2. Draft validation determines whether preparation can advance. Meta campaign
   containers do not depend on a Page ID or a video; ad creation still does.
3. The existing campaign proposal service checks account binding, budget caps,
   channels and deduplication. Proposals remain pending human diff-bound review.
4. Existing approval, execution, reconciliation and braking remain authoritative.
   A model's claim of success, or even proposal state `executed` without a receipt,
   is not evidence of campaign creation. Only a successful execution is shown as
   verified creation. Current delivery status belongs to the Campaigns surface.
5. Activation and messaging are separate authorizations. Total project budget is
   a planning ceiling, **not evidence of a provider-enforced spend limit**.

No rendering rule is specific to a customer, inauguration, model or computer.
Migration 0058 adopts prior runtime jobs by business/launch slug and their owned
drafts, then adopts other drafts individually. Existing IDs, native proposals,
approvals and executions are preserved. Existing source-plan detail remains
available for documents and private video uploads; it is not silently copied or
treated as a new approval.

## Runtime behavior and limits

Both CLI adapters receive the same current workspace snapshot along with the
approved plan and draft context. A complete draft report is promoted by the server
to a pending proposal in the same transaction, using a savepoint: a validation
failure retains the useful draft and reports the exact preparation error. Lost
acknowledgements remain idempotent. The adapter cannot approve or activate.

The local background connector remains a bounded draft-preparation worker, not
an unrestricted autonomous marketing agent. Interactive MCP clients have the
normal scoped tool catalog. Installing only an MCP URL does not install a local
worker or wake a closed chat. Existing opt-in installation/pairing is unchanged.
CLI compatibility and startup checks belong to the adapter; shared state does not
promise identical model responses or native client UI.

Resources in this version are typed planning records and links, not a replacement
for the existing media upload service, landing deployment or WhatsApp provider.
`ready` is an editorial declaration, never an integration test result. Workspace
activity records context changes and proposal preparation; campaign receipts and
runtime statuses are read from their authoritative tables. Personal chat history
is not imported. List limits are explicit (200 projects/campaigns, 100 events,
20 jobs); direct project lookup remains available.

## Regression checks

Test MCP-to-panel-to-other-client context handoff, PATCH omission/null, stale
revision rejection, business isolation, session/CSRF protection, stable creation
keys, concurrent proposal retries, runtime prompt context, no implicit approvals,
and creation evidence independent from model claims. Client compatibility is
checked via the shared wire contract and adapter tests, not by pretending a mocked
handoff is a real installed-client end-to-end test.
