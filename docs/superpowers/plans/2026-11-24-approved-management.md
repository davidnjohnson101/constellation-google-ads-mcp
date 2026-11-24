# Approved Google Ads management implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Extend the existing interactive MCP with positive keyword management, direct negative removal, campaign status, and average daily budget changes, requiring explicit approval.

**Architecture:** Keep the working negative addition tools unchanged. A new `management` namespace uses focused state readers and typed operation builders with a signed, ten-minute approval envelope. Reuse existing interactive OAuth, MCC account policy, and dedicated preview secret; Redis atomically consumes each approval before a live request.

**Tech Stack:** Python, FastMCP, Google Ads protobufs, Redis, Cloud Run.

**Spec:** The user authorized the existing plugin expansion, positive keywords and direct negatives first, with explicit approval for every live change.

## Global constraints

- Same plugin ID, OAuth connection, service, MCC, and audience.
- No shared negative list management, generic mutate proxy, campaign creation/removal, or total budget edits.
- Positive keywords: Search ad groups, add with explicit match and status, pause/enable/remove existing keywords. Negative adds retain existing tools; direct removals require explicit negative=true.
- Campaign status only ENABLED/PAUSED. Daily budget edits change only amount_micros, require explicit matching currency, show all nonremoved campaigns sharing the budget.
- Preview is validate-only and issues an expiring Redis permit. Apply takes a client account lock, rechecks actor, scope, expiry, exact state, Google validation, and consumes the permit with GETDEL. Missing approval data fails closed. No automatic write retry; verify readback and report uncertainty honestly.
- Worker and fallback configurations expose no Ads write tools.
- Preserve currently deployed OAuth repair by extending the serving image.

## Review focus

- A removed or non-keyword criterion must never become a status update or removal of another criterion type.
- Changes to shared-budget membership, campaign status, or currency invalidate approval.
- Decimal amount precision, int64 overflow, and floats fail before any API write.
- Cross-user, cross-action, tampered, replayed, or expired approvals fail closed.
- A committed write with a lost response is reported as observed, never blindly retried.

### Task 1: Typed management and approval behavior

Files: create `ads_mcp/management_state.py`, `ads_mcp/management_mutation.py`, `ads_mcp/tools/management.py`, and `tests/tools/management_test.py`.

Interfaces: preview_add_keywords(customer_id,campaign_id,ad_group_id,keywords,status); preview_set_keyword_status(customer_id,campaign_id,ad_group_id,criterion_id,status); preview_remove_keyword(customer_id,campaign_id,criterion_id,negative,ad_group_id=None); preview_set_campaign_status(customer_id,campaign_id,status); preview_set_daily_budget(customer_id,campaign_id,amount,currency_code); apply_approved_change(preview_token,approved=False).

- [ ] Write behavior tests with real protobufs and fake external Ads/Redis boundaries; run and observe missing module failures.
- [ ] Implement readers, narrowly typed mutations, HMAC envelope, Redis single-use guard, and honest readback.
- [ ] Run management tests and existing negative tests; all pass.
- [ ] Commit implementation and tests.

### Task 2: Discovery, deployment, and documentation

Files: modify interactive/worker YAML, mounting tests, image overlay, deployment script, README.

- [ ] Add discovery and worker-isolation assertions; observe failing discovery.
- [ ] Mount management only in interactive deployment. Copy new modules into both runtime source locations; add explicit management enablement.
- [ ] Run full unit suite, MCP smoke tests, Black, runtime discovery.
- [ ] Commit configuration and documentation.

### Task 3: Review and release

- [ ] Obtain independent whole-branch review; fix important findings with regression tests.
- [ ] Publish reviewable PR and stage the existing Cloud Run service with no traffic.
- [ ] Verify runtime and validation-only behavior, promote authorized update, refresh existing catalog, and verify new read/write tools.
- [ ] Report release, checks, and any remaining limitation. Make no live Ads changes.

## Execution record

- Catalog refreshed in the existing ChatGPT plugin: five read and five write tools including both negatives tools.
- Native implementation selected under the developer instruction to execute authorized work without redundant approval. No live Google Ads mutation is authorized by this feature request.
- Full initial suite: 140 unit tests and two protocol smoke tests passed; Black passed.
- Independent review: fixed overlapping applies with a per-client Redis lock, strict wire booleans, single-use no-ops, positive permits (missing/evicted data fails closed), and serving/template/staged release-setting checks. Regressions reproduced overlap/coercion/no-op and missing deployment helper before their fixes.
