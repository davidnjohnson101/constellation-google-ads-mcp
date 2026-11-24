# Expanded Google Ads writes

Status: proposed design, awaiting the user's review before implementation.

## Intent and agreed scope

Extend the existing Constellation Google Ads MCP into a broad advertising
management connection usable from ChatGPT, Claude, and other compatible remote
MCP clients. The user authorized every capability in the proposed roadmap
except durable change history and recovery. They explicitly selected creation
of both Search and Performance Max/Vehicle Ads campaigns, starting paused.

Keep the existing plugin identity, production MCP URL, Cloud project, Cloud Run
service, Google OAuth connection, account audience, and MCC client-account scope.
Each actual Ads write still requires explicit approval of its exact preview.
Building or releasing tools does not authorize sample changes in live accounts.

The excluded history/recovery feature means no new permanent change-history
database, rollback interface, or automated recovery actions. Existing operational
logs, ten-minute approval permits, concurrency locks, and immediate readback
remain necessary parts of write execution.

## Approach

The proposed approach is one shared approval/execution engine with strictly
typed, resource-specific operations. A bulk plan contains an itemized set of
changes for exactly one client account. Existing focused preview/apply tools
remain compatible facades over the same safeguards. Domain preview helpers may
make common tasks easier; they must produce the same kind of approved plan.

Alternatives considered by the builder:

- Independent preview/apply implementations per resource would repeat signing,
  concurrency, expiry and readback logic. The present negative-add and management
  paths already demonstrate this maintenance cost.
- A generic API mutation or arbitrary field-mask tool would expose resource and
  update paths that the preview layer cannot constrain reliably.
- Shared typed plans preserve precise previews while supporting related creates
  and updates in a single validated request. This is the proposed design choice,
  not an additional preference attributed to the user.

## Feature coverage

| Area | Included behavior |
| --- | --- |
| Bulk plans | Multiple typed creates, updates, associations and removals in one itemized, approved client-account plan. Include the current keyword, negative, campaign-status and daily-budget operations. |
| Ad groups and bidding | Create, rename, pause and enable Search ad groups. Change manual CPC bids on eligible ad groups/positive keywords, supported campaign target CPA/ROAS settings, and supported ad-group target overrides. Explicit standard-strategy changes and client-owned portfolio target changes show their full impact. |
| Search ads | Create and edit responsive search ads, including headlines, descriptions, pinning, paths, final/mobile URLs; pause and enable existing ads. New ads start paused. |
| Assets | Create/edit sitelinks and callouts; create replacement text assets; link/unlink eligible assets at campaign, ad-group and PMax asset-group levels. Reuse existing image/logo/video assets owned by the client. Show all affected associations when changing a shared ad or mutable asset. |
| Targeting and settings | Add/remove campaign location targets and exclusions, language targets, replace ad schedules, set supported device adjustments, edit eligible campaign names/dates and Search network settings. Show effective reach changes and use the account's time zone. |
| Shared negatives | Create/rename shared negative keyword lists; add/remove their keyword criteria; attach/detach supported campaigns; remove a list only with explicit treatment of its attachments. Resolve list identity and every affected campaign before approval. |
| Campaign creation | Create Search and PMax campaigns with a new explicitly nonshared daily budget, explicit bidding and targeting. PMax supports complete nonretail asset setups and retail/Vehicle Ads using an existing linked Merchant Center account and explicit inventory selection. All new campaigns start paused. |
| PMax setup | Create, rename, pause and enable asset groups; manage complete asset-link sets and valid retail listing-group trees. Preserve Google's campaign-level brand-asset requirements. |

These definitions bound this release. Customer access/billing, Merchant Center
account/feed provisioning, audience/contact-list uploads, image/video generation
or binary media upload, conversion setup, and other campaign-creation types are
not silently added to it. Existing uploaded media can be reused by asset ID.
Removing campaigns, ad groups or ads is not added by this design; pause remains
the status control. Existing keyword removal and explicit shared-list/association
removals remain included.

## Bulk execution and previews

Proposed builder defaults:

1. One customer per plan, up to 100 generated Google Ads mutation operations.
   A higher-level create may expand into several operations and counts toward
   this limit. MCC-wide work uses separate visible plans and approvals per client;
   there is no promise of a transaction across accounts.
2. Use synchronous `GoogleAdsService.Mutate`, `partial_failure=False`, and
   `validate_only=True` for whole-plan validation. Do not use asynchronous batch
   jobs in this release: they lack validate-only and use partial failure.
3. New resources have explicit plan-local aliases. The server assigns globally
   unique negative temporary IDs, orders dependencies, and resolves aliases to
   real resource IDs in the result. Raw resource payloads, arbitrary API method
   names and arbitrary update masks are never caller inputs.
4. Reject duplicate/conflicting edits to the same field, missing aliases,
   dependency cycles, foreign-account resources, ambiguous resource identity and
   attempts to mutate unsupported resource types. Combine only deterministic,
   compatible intents; never silently choose between conflicting requests.
5. Show each entity's account/campaign/ad group, name and ID, operation, before
   and after values, every shared association affected, total operation count,
   currency/time zone where relevant, and a complete summary of newly created
   resources. New resources use their plan alias until Google assigns an ID.
6. Existing values, effective bid strategy, relevant parent state, shared
   membership and dependency resources are part of the signed snapshot. Changes
   to any relevant value invalidate the preview instead of overwriting them.
7. Remove no-op updates from the mutation list but list them as unchanged in the
   preview. Empty plans perform no Ads mutation and their approval is consumed.

## Bidding and shared effects

Read the effective bidding strategy before building a bid operation. Manual CPC
edits are offered only where Google applies them; a valid-but-ignored bid must
not be presented as an effective change. CPA/ROAS values are mapped to the correct
supported strategy fields for the deployed API version and campaign type.

Strategy switches must be explicit and show the old and new strategy plus any
cleared or newly set targets. Changing a portfolio strategy requires a separate
typed portfolio operation, client ownership, and a snapshot of every attached
campaign. A campaign-specific request never silently edits a portfolio shared by
other campaigns or switches that campaign away from its portfolio.

Money and bid amounts use exact decimal currency strings, never binary floats.
ROAS uses an explicit ratio with a percentage display in the preview. No fields
are guessed from UI labels. Account currency and supported field constraints are
checked before Google validation.

Content updates to a reused `Ad` or mutable `Asset` require a preview of all
active associations. Immutable text/image assets use explicit replacement and
association changes. System-generated assets are not modified. Readback compares
only the approved configuration; changing Google policy/learning status must not
be mistaken for a configuration mismatch or a promise that an ad will deliver.

## Targeting, schedules and shared negatives

Resolve location and language constants, selected criterion IDs and their parent
relationships. Additions/removals are type-specific. For schedule replacement,
preview the complete weekly schedule in the customer's time zone; validate day,
hour and supported minute boundaries and handle overnight ranges explicitly.

Previews disclose the effective result of removing the final location, language
or schedule restriction. No empty list is silently interpreted as an instruction
to broaden reach. Explicit all-location/all-language/all-hours intents are shown
as such. Search network controls and device bid adjustments are gated by actual
campaign and strategy support.

Shared-list previews include contents relevant to the edit and all attached
campaigns, including paused ones. Only negative-keyword list types are accepted.
Google's supported campaign types are enforced. In particular, an unsupported
PMax shared-list attachment fails clearly; it is never converted into an
account-level negative or a set of direct negatives without a separate plan.
Account-level negative-list administration is not part of this release.

## New campaign setup

Campaign creation requires a name, campaign type, explicit daily amount and
account currency, supported bidding settings, explicit location/language scope,
and an explicit EU political-advertising declaration. Optional dates are checked
against the customer's calendar/time zone. A fresh, nonshared budget is the
builder default; reusing/reassigning an existing budget is not inferred.

New campaigns, Search ad groups/ads and PMax asset groups start paused. The plan
cannot create and activate the same resource in one approval. A later activation
uses real IDs, a fresh snapshot and a separate approved preview.

Search campaign plans may include their ad groups, positive/negative keywords,
responsive search ads and eligible asset associations in the same transaction.

For PMax:

- Support the eligible Maximize Conversions / Maximize Conversion Value families
  and their supported targets. Do not assign an unsupported portfolio strategy.
- Preserve brand guidelines and require the correct business-name/logo links.
  No implicit disabling of brand guidelines to make validation pass.
- Nonretail asset groups require a complete minimum asset pack, including
  existing eligible media assets, with all links created atomically.
- Retail/Vehicle Ads require an existing linked Merchant Center account, feed
  selection, and an explicit valid listing-group tree. Vehicle creation sets
  `listing_type=VEHICLES` on a supported API version. Do not use the obsolete
  `shopping_setting.use_vehicle_inventory` switch.
- A retail group can explicitly use Merchant Center generated assets without
  custom creative, or supply a complete custom asset pack. Partial unsupported
  creative packs are rejected rather than producing an uneditable campaign.
- Inventory scope is explicit. An all-inventory tree must be requested and shown;
  a supplied subset is not widened to all vehicles/products.

A successful create means Google accepted and readback verified the paused
configuration. It does not promise feed approval, policy approval or delivery.
Missing Merchant Center links/assets/eligibility are returned as actionable
prerequisites, not provisioned or bypassed automatically.

## Approval, verification and client portability

Every apply remains a non-read-only, destructive-capable MCP tool with strict
boolean approval defaulting to false. Its input is the opaque preview token and
the explicit host approval assertion. ChatGPT/Claude must ask the user before
the apply call; `approved=true` is not independent proof of a human click.

Use the same interactive Google OAuth and live active-client MCC checks. Signed
ten-minute previews bind caller, manager, exact plan and snapshot. Keep positive
Redis permits (missing/expired/evicted permits fail closed), atomically consume
them once, and hold a per-client lock from final snapshot through verification.
Migrate the existing direct-negative path to these common concurrency/approval
guards while preserving its named tool contracts. Existing preview tokens may
require a new preview across the upgrade; OAuth credentials are not rotated.

Recheck access, snapshot, validation, expiry and lock ownership before one live
request. Disable automatic mutation retries. Immediately read back each created,
updated or removed resource and compare only approved fields plus relationships.
Return per-item verified results, real resource IDs, and honest overall states:
applied, observed-applied, uncertain or no-change. A lost response never triggers
another create. Without durable history, callers retain the returned result;
this release adds no persistent status/recovery service.

Keep the protocol usable with the same public `/mcp` URL. The live service's
OAuth metadata currently advertises client registration, PKCE S256 and client-ID
metadata support. Claude should use its remote-connector OAuth flow. Host tool
permission settings are configured separately; ChatGPT permissions do not grant
Claude permission. Do not require a ChatGPT-only UI or a second server.

## Delivery and acceptance

Implementation is split into reviewable increments: common bulk engine; ad
groups/bids; ads/assets/PMax groups; targeting/settings; shared lists; campaign
creation; final integration and release. This is one authorized scope, not a
request to choose only one increment. Individual files stay focused by domain.

Use actual Google Ads protobufs with fake Ads/Redis boundaries for tests. Cover
atomic validation, generated IDs/dependencies, every operation family, type and
ownership rejection, complete shared effects, unsupported/ignored bid behavior,
strict booleans, stale plans, replay, concurrency, expiry, lost responses and
per-item verification. Keep current regression tests and worker isolation.

Run the full unit suite, real MCP protocol/schema smoke tests, formatting and
independent code review. Preserve the deployed OAuth repair by extending the
currently serving image; copy all modules to both runtime import locations.
Verify serving/template/staged settings, stage the same service at zero traffic,
and obtain approval of the concrete PR/release before merging and promotion.
Refresh the existing plugin catalog after release. Live acceptance uses only
read and validate-only previews unless the user separately approves a precise
real account change. A Claude end-to-end connection remains an explicit test;
public OAuth metadata alone is not proof that Claude has connected successfully.

## Primary references checked

- Google mutate overview: https://developers.google.com/google-ads/api/docs/mutating/overview
- Temporary IDs/ordering: https://developers.google.com/google-ads/api/docs/mutating/best-practices
- Shared sets: https://developers.google.com/google-ads/api/docs/targeting/shared-sets
- Bidding types: https://developers.google.com/google-ads/api/docs/campaigns/bidding/strategy-types
- RSA edits: https://developers.google.com/google-ads/api/docs/responsive-search-ads/mutate-ads
- Assets: https://developers.google.com/google-ads/api/docs/assets/working-with-assets
- PMax campaign/brand setup: https://developers.google.com/google-ads/api/performance-max/create-campaign
- Retail/Vehicle Ads: https://developers.google.com/google-ads/api/performance-max/retail
- PMax asset groups: https://developers.google.com/google-ads/api/performance-max/asset-groups
- Retail listing groups: https://developers.google.com/google-ads/api/performance-max/listing-groups
- Claude remote connectors: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
