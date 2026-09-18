# Optional business context and launch review

These capabilities are independent. Catalogue credentials are not required to review a launch.

## Catalogue connection

Set `ADS_STORE_API_BASE_URL` to a fixed HTTPS API root, without `/v1/catalog`,
credentials, query or fragment. Optional `ADS_STORE_API_EGRESS_IP` is the verified
outbound address shown to the owner for provider allowlisting. The engine has no
default provider. The owner enters the bearer token under Connections → Catalogue
and stock; it is never configured through a model-visible tool.

The connector probes GET `/v1/catalog`, `/v1/store-catalog` and `/v1/stock` before
saving. Responses use `{data: [], pagination: {page, per_page, total, total_pages}}`.
Store rows may include a nested `product`. MCP tools `get_store_api_status` and
`get_store_catalog` are read-only and business-scoped. Each request is bounded to
200 records and 2 MiB; no redirects, private-address resolution or arbitrary paths.
Only allowlisted visible fields are returned. OAuth client-credentials token
issuance is not implemented; this connector uses the owner-provided bearer token.

Credentials are encrypted with a dedicated derived key and AAD binding the
business and origin. Changing the configured origin disconnects the stored token.
Successful save/replacement appends an audit entry without credentials. An invalid
replacement leaves the previous credential intact. Neither status nor read tools
return the stored token. Product descriptions are data, not agent instructions.

## Launch review packs

Mount a read-only kit using `ADS_KIT_DIR`. A pack lives at
`launches/<slug>/manifest.json` and declares `slug`, `business_id`, `title`,
`summary`, `documents` (title/file), `video_slots` (id/title/format/copy), and
`blockers`. Optional `proposal_id` links an existing native proposal; it does not
create one. The panel displays a plan even before native provider assets exist.

Set `public_preview: true` only when intentionally exposing `landing.html` at
`/eventos/<slug>`. Files named in `public_assets` are publicly accessible; every
other file, including the manifest and strategy, remains private. Landing scripts
and styles must respect the deployment CSP. Preview publication does not implement
lead collection or outbound messaging.

The owner may approve the exact content revision. The audit record is editorial
sign-off only, never a signed provider authorization and never spending approval.
Changing the manifest, documents, landing or public assets invalidates that sign-off.

MP4 slot uploads are private, authenticated, limited to 100 MiB and basic MP4
container identification. Each slot is write-once; no accidental overwrite. Files
live under the existing backed-up creative asset volume. Uploading does not send
the video to Meta; the actual provider package must be prepared and approved
separately. Do not label a review pack as an executable campaign.
