"""Preview-first keyword, campaign-status and daily-budget management."""

import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import contextmanager
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import StrictBool

from ads_mcp import management_state as state, utils
from ads_mcp.management_mutation import mutate
from ads_mcp.tools import negatives as n

management_mcp = FastMCP("management")
Status = Literal["ENABLED", "PAUSED"]
_ACTIONS = {
    "add_keywords",
    "keyword_status",
    "remove_keyword",
    "campaign_status",
    "daily_budget",
}


def _context(customer):
    if (
        os.environ.get("GOOGLE_ADS_MCP_ENABLE_MANAGEMENT_WRITES", "").lower()
        != "true"
    ):
        raise ToolError("Management writes are not enabled on this deployment.")
    if not os.environ.get("GOOGLE_ADS_MCP_STORAGE_REDIS_URL"):
        raise ToolError(
            "Durable Redis approval consumption must be configured."
        )
    caller, key, manager = n._context(customer)
    return (
        caller,
        hmac.digest(key, b"constellation-management-v1", "sha256"),
        manager,
    )


def _store():
    import redis

    return redis.Redis.from_url(
        os.environ["GOOGLE_ADS_MCP_STORAGE_REDIS_URL"],
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def _permit(preview_id: str, expires_at: int):
    """Missing or evicted approval data must fail closed, never allow replay."""
    try:
        with _store() as store:
            created = store.set(
                f"constellation:ads-management:permit:{preview_id}",
                "1",
                nx=True,
                ex=max(1, expires_at - int(time.time())),
            )
            if not created:
                raise ValueError("Preview permit already exists")
    except Exception as exc:
        raise ToolError(
            "Approval storage is unavailable. No changes were made."
        ) from exc


def _claim(preview_id: str, expires_at: int):
    """Atomically consume an issued permit across all Cloud Run instances."""
    try:
        with _store() as store:
            claimed = store.getdel(
                f"constellation:ads-management:permit:{preview_id}"
            )
    except Exception as exc:
        raise ToolError(
            "Approval storage is unavailable. No live write was attempted."
        ) from exc
    if claimed not in (b"1", "1"):
        raise ToolError(
            "This preview is missing, expired or already consumed. Generate and approve a new preview."
        )


@contextmanager
def _guard(customer: str):
    """Serialize applies per client, including shared-budget campaign aliases."""
    try:
        store = _store()
        lock = store.lock(
            f"constellation:ads-management:apply:{customer}",
            timeout=n._TTL + 120,
            blocking=False,
        )
        acquired = lock.acquire(blocking=False)
    except Exception as exc:
        raise ToolError(
            "Approval locking is unavailable. No live write was attempted."
        ) from exc
    if not acquired:
        store.close()
        raise ToolError(
            "Another change is being applied in this account. Generate and approve a fresh preview after it finishes."
        )
    try:
        yield lock
    finally:
        try:
            lock.release()
        except Exception:
            utils.logger.warning(
                "Management account lock could not be released; it will expire."
            )
        store.close()


def _status(value):
    if value not in {"ENABLED", "PAUSED"}:
        raise ToolError("status must be ENABLED or PAUSED.")
    return value


def _preview(action: str, selected: dict, change: dict) -> dict:
    caller, key, manager = _context(selected["customer_id"])
    client = utils.get_googleads_client()
    before = state.snapshot(client, action, selected, change, manager)
    if action == "add_keywords":
        present = {n._keyword_key(k) for k in before["keywords"]}
        change["additions"] = [
            k for k in change["keywords"] if n._keyword_key(k) not in present
        ]
    elif action in {"keyword_status", "remove_keyword"}:
        found = before["keywords"]
        if len(found) != 1 or found[0]["negative"] != change["negative"]:
            raise ToolError(
                "The selected keyword was not found with the specified positive/negative type."
            )
    elif action == "daily_budget":
        if change["currency_code"] != before["target"]["currency_code"]:
            raise ToolError(
                "currency_code must exactly match the Google Ads account currency."
            )
        if (
            before["budget"]["period"] != "DAILY"
            or before["budget"]["total_amount_micros"]
        ):
            raise ToolError(
                "Only existing average DAILY budgets are supported; total budgets require a separate workflow."
            )
    noop = state.satisfied(before, before, action, change)
    if not noop:
        try:
            mutate(client, action, before, change, True)
        except Exception as exc:
            raise ToolError(
                "Google Ads rejected validation. No changes were made; correct the request and preview again."
            ) from exc
    now = int(time.time())
    payload = {
        "version": 1,
        "preview_id": str(uuid.uuid4()),
        "issued_at": now,
        "expires_at": now + n._TTL,
        "caller": caller,
        "manager": manager,
        "action": action,
        "target": selected,
        "before": before,
        "change": change,
        "noop": noop,
    }
    encoded = n._encode(payload, key)
    if len(encoded) > 262144:
        raise ToolError(
            "This preview is too large. Reduce the keyword batch or select a smaller shared budget."
        )
    _permit(payload["preview_id"], payload["expires_at"])
    return {
        "status": "no_change" if noop else "validated",
        "action": action,
        "target": before["target"],
        "before": before,
        "change": change,
        "preview_id": payload["preview_id"],
        "expires_at": payload["expires_at"],
        "preview_token": encoded,
        "approval_required": True,
        "google_ads_changes_made": False,
    }


def preview_add_keywords(
    customer_id: str,
    campaign_id: str,
    ad_group_id: str,
    keywords: list[n.NegativeKeyword],
    status: Status,
) -> dict:
    """Validate positive Search keywords without writing. Show destination, text,
    explicit match type and ENABLED/PAUSED status; obtain approval before apply.
    Existing identical positives are skipped, even if their status differs.
    Text and match changes require separately approved remove/add operations.
    """
    normalized, duplicates = n._keywords(keywords)
    return _preview(
        "add_keywords",
        state.selection(customer_id, campaign_id, ad_group_id),
        {
            "keywords": normalized,
            "status": _status(status),
            "negative": False,
            "duplicates_removed": duplicates,
        },
    )


def preview_set_keyword_status(
    customer_id: str,
    campaign_id: str,
    ad_group_id: str,
    criterion_id: str,
    status: Status,
) -> dict:
    """Preview pause/enable of one existing positive Search keyword. Show its
    exact text, match type and before/after status and obtain explicit approval.
    Negative criteria cannot be paused; use an approved direct removal instead.
    """
    return _preview(
        "keyword_status",
        state.selection(customer_id, campaign_id, ad_group_id),
        {
            "criterion_id": n._id(criterion_id, "criterion_id"),
            "negative": False,
            "status": _status(status),
        },
    )


def preview_remove_keyword(
    customer_id: str,
    campaign_id: str,
    criterion_id: str,
    negative: StrictBool,
    ad_group_id: str | None = None,
) -> dict:
    """Preview removing one positive Search keyword or direct campaign/ad-group
    negative. Explicitly identify negative=true/false; display exact criterion,
    text, match and destination for approval. Does not edit shared negative lists.
    Removal cannot restore the original criterion ID; re-add needs new approval.
    """
    if type(negative) is not bool:
        raise ToolError("negative must be explicitly true or false.")
    return _preview(
        "remove_keyword",
        state.selection(customer_id, campaign_id, ad_group_id),
        {
            "criterion_id": n._id(criterion_id, "criterion_id"),
            "negative": negative,
        },
    )


def preview_set_campaign_status(
    customer_id: str, campaign_id: str, status: Status
) -> dict:
    """Preview campaign ENABLED/PAUSED only. Display account, campaign, current
    budget/currency and before/after status. Enabling may resume advertising spend.
    Obtain explicit approval of this preview before apply; never removes campaigns.
    """
    return _preview(
        "campaign_status",
        state.selection(customer_id, campaign_id),
        {"status": _status(status)},
    )


def preview_set_daily_budget(
    customer_id: str, campaign_id: str, amount: str, currency_code: str
) -> dict:
    """Preview an existing average DAILY budget amount in exact decimal currency
    units, e.g. amount='125.50', currency_code='USD'. Show old/new amount and EVERY
    affected campaign, including other campaigns sharing this budget, for explicit
    approval. Average daily budget is not a hard daily spend cap. Only amount_micros
    changes; no total budgets, new budgets or sharing/assignment changes.
    """
    return _preview(
        "daily_budget",
        state.selection(customer_id, campaign_id),
        {
            "amount": amount,
            "amount_micros": state.amount_micros(amount),
            "currency_code": currency_code,
        },
    )


def apply_approved_change(
    preview_token: str, approved: StrictBool = False
) -> dict:
    """Apply ONLY a displayed management preview after explicit user approval.
    approved=true asserts the host obtained that specific approval; it is not
    independent proof of a human click. Same caller, ten-minute expiry, unchanged
    live state, revalidation and durable single-use consumption are mandatory.
    No automatic write retry. Report uncertainty and obtain a fresh preview before
    any further attempt. Existing negative-add previews use their own apply tool.
    """
    if approved is not True:
        raise ToolError(
            "Explicit approval of the displayed preview is required."
        )
    body, signature, payload = n._decode(preview_token, max_length=262144)
    try:
        caller, key, manager = _context(
            n._id(payload["target"]["customer_id"], "customer_id", True)
        )
        expected = hmac.new(
            key, n._DOMAIN + body.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ToolError(
                "Invalid management preview signature. Preview again."
            )
        now = int(time.time())
        if caller != payload["caller"] or manager != payload["manager"]:
            raise ToolError(
                "This preview belongs to a different caller or manager."
            )
        if (
            not payload["issued_at"] <= now < payload["expires_at"]
            or payload["expires_at"] - payload["issued_at"] != n._TTL
        ):
            raise ToolError(
                "This preview expired. Generate and approve a new preview."
            )
        if payload["action"] not in _ACTIONS:
            raise ValueError()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("Invalid management preview. Preview again.") from exc
    action, selected, before, change = (
        payload[k] for k in ("action", "target", "before", "change")
    )
    client = utils.get_googleads_client()
    with _guard(selected["customer_id"]) as lock:
        return _apply_locked(client, payload, manager, lock)


def _apply_locked(client, payload: dict, manager: str, lock) -> dict:
    action, selected, before, change = (
        payload[k] for k in ("action", "target", "before", "change")
    )
    live = state.snapshot(client, action, selected, change, manager)
    if live != before:
        raise ToolError(
            "The account, target or affected state changed since preview. Generate and approve a new preview."
        )
    if payload["noop"]:
        _claim(payload["preview_id"], payload["expires_at"])
        return {
            "status": "no_change",
            "verified": True,
            "google_ads_changes_made": False,
            "preview_id": payload["preview_id"],
        }
    try:
        mutate(client, action, before, change, True)
    except Exception as exc:
        raise ToolError(
            "Google Ads rejected revalidation. No live change was attempted."
        ) from exc
    if int(time.time()) >= payload["expires_at"]:
        raise ToolError(
            "The preview expired during validation. Generate and approve a new preview."
        )
    _claim(payload["preview_id"], payload["expires_at"])
    if int(time.time()) >= payload["expires_at"]:
        raise ToolError(
            "The preview expired before the write. Generate and approve a new preview."
        )
    try:
        owned = lock.owned()
    except Exception as exc:
        raise ToolError(
            "Approval lock could not be verified. No live write was attempted."
        ) from exc
    if not owned:
        raise ToolError(
            "The approval lock was lost. Generate and approve a new preview."
        )
    response = None
    try:
        response = mutate(client, action, before, change, False)
    except Exception:
        pass
    observed = None
    try:
        observed = state.snapshot(client, action, selected, change, manager)
    except Exception:
        pass
    verified = observed is not None and state.satisfied(
        before, observed, action, change
    )
    count = len(change["additions"]) if action == "add_keywords" else 1
    returned = (
        [r.resource_name for r in response.results]
        if response is not None
        else []
    )
    if action == "add_keywords":
        keys = {n._keyword_key(k) for k in change["additions"]}
        resources = (
            {
                k["resource_name"]
                for k in observed["keywords"]
                if n._keyword_key(k) in keys
            }
            if observed
            else set()
        )
    else:
        resources = {
            (
                before["budget"]["resource_name"]
                if action == "daily_budget"
                else (
                    before["target"]["campaign_resource"]
                    if action == "campaign_status"
                    else before["keywords"][0]["resource_name"]
                )
            )
        }
    confirmed = (
        len(returned) == count
        and len(set(returned)) == count
        and set(returned) == resources
    )
    status = (
        "applied"
        if verified and confirmed
        else "observed_applied" if verified else "uncertain"
    )
    utils.logger.info(
        "management_apply %s",
        json.dumps(
            {
                "preview_id": payload["preview_id"],
                "customer_id": selected["customer_id"],
                "campaign_id": selected["campaign_id"],
                "action": action,
                "status": status,
            }
        ),
    )
    return {
        "status": status,
        "action": action,
        "preview_id": payload["preview_id"],
        "verified": verified,
        "observed": observed,
        "google_ads_changes_made": True if confirmed else None,
        "automatic_retry_performed": False,
    }


for preview in (
    preview_add_keywords,
    preview_set_keyword_status,
    preview_remove_keyword,
    preview_set_campaign_status,
    preview_set_daily_budget,
):
    management_mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    )(preview)
management_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)(apply_approved_change)
