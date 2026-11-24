# Add negative keywords

The user approved the existing preview -> validate -> approve -> apply ->
verify approach, selecting negative keywords as the first write action and
confirming both campaign and ad-group levels on 2026-11-24. They subsequently
requested all client accounts under the connected manager rather than a pilot.

## Behavior

Expose `negatives_preview_add_negative_keywords` and
`negatives_apply_add_negative_keywords` in the interactive MCP. A preview
targets one customer and campaign, optionally one ad group, and accepts up
to 100 bare keyword texts with explicit EXACT, PHRASE, or BROAD match types.
Show account/campaign/ad-group names and IDs, additions and existing items.
Deduplicate repeated input and existing direct negatives without creating
anything. Validate pending creates with Google's `validate_only`.

Return a signed preview valid for ten minutes. Bind it to the authenticated
upstream Google access token, exact target and additions, and target state. Applying
requires that preview and explicit approval of its displayed changes.
Reauthorize and reread the target and existing negatives before applying.
Never add something omitted from the approved additions. Use an atomic
create-only request with automatic retries disabled. Read back the requested
negatives and report verified, already present, or uncertain outcomes honestly.

## Boundaries

Keep the existing worker read only. Writes require explicit deployment
enablement, explicit all-MCC mode (optionally narrowed by an allowlist), live
MCC membership checks, and a dedicated preview signing key.
Reject service-JWT and offline credentials. Do not expose arbitrary mutations,
shared lists, account-wide exclusions, removals or edits. Use existing Google
Ads Python services and MCP naming/configuration patterns. No new database,
OAuth scope or portal UI is required for this first interactive action.
Keep existing OAuth/storage keys unchanged. Campaign scope supports Search,
Shopping and Performance Max; ad-group scope supports Search. Reject other
channels rather than imply unsupported match semantics.

## Approaches

The user selected a narrow write action with preview and approval. A generic
mutation proxy exposes unnecessary resource types. A portal execution system
would add a second product surface and approval identity contract; defer it.
The builder chooses signed short-lived previews rather than in-memory state
so Cloud Run instances can validate previews consistently. Replay rereads live
state; it does not promise globally exactly-once mutation under concurrency.

## Validation and release

Use real Google Ads protobufs with fake external transports in tests. Exercise
both target levels, duplicate input/live negatives, wrong account or bearer,
tampered/expired previews, changed target, failed validation, timeout and failed
readback. Assert worker tool discovery never exposes negatives. Run the full
unit and MCP smoke suites, format checks, then review the branch. Prepare a PR
and stage a zero-traffic revision extending the exact serving image to retain
the unmerged production OAuth repair and its dependencies. Deployment changes
shared plugin write capabilities; prepare a concrete release before the final
approval if needed.
