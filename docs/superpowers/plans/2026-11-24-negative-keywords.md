# Negative Keywords Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add previewed, explicitly approved campaign and ad-group negative keyword creation.

**Architecture:** A new negatives tool module constructs create-only criteria and validates
them with Google. Signed, caller-bound ten-minute previews carry approved additions;
apply rechecks access and live state and verifies results. Existing workers remain read only.

**Tech Stack:** Python, FastMCP, Google Ads Python protobuf/client services, unittest.

**Spec:** ../specs/2026-11-24-negative-keywords-design.md

## Global Constraints

- At most 100 keywords, each with explicit EXACT, PHRASE, or BROAD match type.
- Writes require interactive auth, deployment enablement, live MCC scope and a dedicated signing key.
- No generic mutations, shared-list/account-wide writes, edits, removals, or worker writes.
- Apply only signed additions; disable automatic retries and partial failure.
- Verify live state; ambiguous outcomes must not be called successful creation.

## Review Focus

- Caller or account changes cannot reuse a preview.
- Existing negatives and duplicate inputs must not cause additional creates.
- A stale preview cannot broaden its original additions or target.
- A timeout or incomplete readback cannot become a fabricated success.
- Worker and default deployments remain unable to mutate Google Ads.

### Task 1: Preview and application behavior

**Files:** Create ads_mcp/tools/negatives.py and tests/tools/negatives_test.py.

**Interfaces:** preview_add_negative_keywords(customer_id, campaign_id, keywords,
ad_group_id=None) -> dict; apply_add_negative_keywords(preview_token, approved=False) -> dict.

- [x] Write behavioral tests with real Google Ads messages and a fake external boundary.
- [x] Run tests and observe the missing-feature failure.
- [x] Implement validation, authorization, signed previews and create-only verified application.
- [x] Run the negative keyword tests until green.

### Task 2: MCP exposure and worker isolation

**Files:** ads_mcp/tools_config.yaml, ads_mcp/worker_tools_config.yaml,
tests/tools_mounting_test.py, tests/smoke/golden_tools_list.json.

- [x] Test interactive tool schemas/annotations and absent worker tools.
- [x] Run and observe the missing-registration failure.
- [x] Mount negatives explicitly in the interactive config; disable them in worker config.
- [x] Regenerate tool discovery golden; run mounting and smoke tests.

### Task 3: Documentation, verification and release

**Files:** README.md, docs/negative-keywords.md, scripts/deploy_negative_keywords.ps1.

- [x] Document approval instructions, environment opt-in, outcomes and deployment command.
- [x] Run full unit and smoke suites plus Black and git diff checks.
- [x] Obtain a focused whole-branch review and fix significant findings with regression tests.
- [x] Prepare branch/PR and exact deployment readiness; report any unverified live behavior.
