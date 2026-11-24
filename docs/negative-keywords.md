# Negative keyword writes

Two interactive MCP tools add direct negative keywords:

- `negatives_preview_add_negative_keywords`: read the destination and existing
  direct negatives, skip duplicates, validate proposed creates with Google,
  and return the exact additions plus a signed ten-minute preview.
- `negatives_apply_add_negative_keywords`: apply the signed additions after
  explicit user approval, recheck access and destination, and read them back.

One preview targets one customer and campaign, optionally one ad group.
Campaign scope supports Search, Shopping and Performance Max; ad-group scope
supports Search. Google validation still determines availability on the
particular account and API version. Display/Video and other campaign types
are rejected because their exclusion semantics differ.

Each of up to 100 keywords requires bare `text` and an explicit `match_type`:
`EXACT`, `PHRASE` or `BROAD`. Do not embed match brackets or quotes in text.
There are no update, removal, generic mutation, shared-list or account-wide
negative actions. Duplicate checks cover direct negatives at the destination;
they do not exhaustively evaluate inherited/shared exclusions.

## Host approval contract

1. Resolve IDs through existing read tools. Call preview with exact IDs.
2. Show the user account name/ID, campaign name/ID, ad group if present,
   every proposed keyword and match type, and skipped existing items.
3. Obtain explicit approval for that displayed preview.
4. Call apply with its unchanged `preview_token` and `approved: true`.
   Never infer approval from analysis enrollment or recommendation publishing.
5. Report only the returned outcome. `applied` requires matching Google results
   and readback. `already_present` made no new changes. `observed_present`
   confirms live state but does not claim who created it. `uncertain` requires
   investigation; do not automatically repeat a write.

The approval flag is a host assertion, not an independently authenticated
human approval service. Previews are bound to the upstream Google access
token; OAuth refresh or reconnect may require a new preview. The token is
signed, not encrypted; do not publish it in logs or share it between users.
Replays reread current negatives. This is not a globally exactly-once system
under concurrency, removal and re-addition, or ambiguous network outcomes.

## Deployment gates

Both tools fail closed unless all gates hold:

- Interactive Google OAuth; no service JWT or offline worker credentials.
- `GOOGLE_ADS_MCP_ENABLE_NEGATIVE_KEYWORD_WRITES=true`.
- A dedicated Secret Manager-backed `GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY`
  containing at least 32 characters of random material. Keep OAuth/storage
  signing and encryption keys unchanged.
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID` identifies the trusted manager account.
- To enable all active client descendants, set
  `GOOGLE_ADS_MCP_NEGATIVES_ALLOW_MCC_DESCENDANTS=true`.
- Alternatively set `GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS` to a
  comma-separated client allowlist. When set, it narrows the all-MCC setting.
- Any existing `GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS` restriction also applies.

Every preview and apply queries the live MCC hierarchy and verifies an enabled,
non-manager client under that MCC. Google enforces the signed-in user's Ads
role; Standard or Admin is required for campaign edits. The analysis worker's
configuration explicitly disables the negatives namespace, and runtime checks
reject its credentials even if a configuration is changed accidentally.

## Release

The observed live service was `google-ads-mcp-authfix-20261112` in
`durable-stack-502219-q0/us-central1`. Its image includes an OAuth persistence
repair from the unmerged `codex/ads-persistent-oauth-redacted-logs` branch.
Use `Dockerfile.negatives` and `cloudbuild.negatives.yaml` to extend the exact
serving image. A plain rebuild with the root Dockerfile would not preserve
that repair. The deployment script pins the currently serving digest, keeps
the existing OAuth and Redis configuration, adds a dedicated preview secret,
and stages a revision with zero production traffic.
The source package pins FastMCP 3.2 and MCP SDK 1.x so the checked protocol
snapshots remain reproducible. The overlay build checks the actual preserved
runtime can import and expose both new tools before it produces an image.

Run `scripts/deploy_negative_keywords.ps1` from this repository using an
authenticated Google Cloud CLI. It requires permission to build images,
deploy Cloud Run revisions and configure the new signing secret. It does
not add any live keywords. Inspect the staged revision, confirm OAuth and
tool discovery, then validate an approved test request without execution.
Promote only after that check with the exact command the script prints.
Refresh/reconnect the plugin to discover the new tools and update its display
description to include approved negative keyword additions.

The existing `auth/adwords` OAuth scope covers reads and writes; no extra
Google OAuth scope is requested. Sources:
[Google access model](https://developers.google.com/google-ads/api/docs/oauth/access-model),
[criteria](https://developers.google.com/google-ads/api/docs/targeting/criteria),
[validation](https://developers.google.com/google-ads/api/docs/concepts/api-structure).
