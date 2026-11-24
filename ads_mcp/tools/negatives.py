"""Preview and explicitly approved creation of direct negative keywords."""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
import uuid
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from ads_mcp import utils

negatives_mcp = FastMCP("negatives")
_DOMAIN = b"constellation-negative-keywords-v1:"
_TTL = 600


class NegativeKeyword(BaseModel):
    """Bare keyword text; match type is always explicit."""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str
    match_type: Literal["EXACT", "PHRASE", "BROAD"]


def _id(value: str, name: str, customer: bool = False) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a numeric string.")
    normalized = value.strip().replace("-", "") if customer else value.strip()
    pattern = r"[0-9]{10}" if customer else r"[1-9][0-9]{0,18}"
    if not re.fullmatch(pattern, normalized):
        raise ToolError(f"{name} is not a valid numeric ID.")
    return normalized


def _context(customer_id: str) -> tuple[str, bytes, str]:
    """Check deployment and caller before any Google API request."""
    if (
        os.environ.get(
            "GOOGLE_ADS_MCP_ENABLE_NEGATIVE_KEYWORD_WRITES", ""
        ).lower()
        != "true"
    ):
        raise ToolError(
            "Negative keyword writes are not enabled on this deployment."
        )
    if os.environ.get(
        "GOOGLE_ADS_MCP_AUTH_MODE", "google_oauth"
    ) != "google_oauth" or any(
        os.environ.get(name)
        for name in (
            "GOOGLE_ADS_SERVICE_REFRESH_TOKEN",
            "GOOGLE_ADS_SERVICE_OAUTH_CLIENT_ID",
            "GOOGLE_ADS_SERVICE_OAUTH_CLIENT_SECRET",
        )
    ):
        raise ToolError(
            "Negative keyword writes require interactive Google OAuth."
        )
    token = get_access_token()
    if token is None or not token.token:
        raise ToolError("An authenticated interactive caller is required.")
    secret = os.environ.get("GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY", "")
    if len(secret) < 32:
        raise ToolError(
            "A dedicated negative-keyword signing key of at least 32 characters is required."
        )
    manager = _id(
        os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""),
        "login customer",
        True,
    )
    if customer_id == manager:
        raise ToolError("Select a client account, not the manager account.")
    raw = os.environ.get("GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS", "")
    allowed = {
        _id(v, "allowed customer", True) for v in raw.split(",") if v.strip()
    }
    all_clients = (
        os.environ.get(
            "GOOGLE_ADS_MCP_NEGATIVES_ALLOW_MCC_DESCENDANTS", ""
        ).lower()
        == "true"
    )
    if (allowed and customer_id not in allowed) or (
        not allowed and not all_clients
    ):
        raise ToolError(
            "This customer is outside the enabled negative keyword account scope."
        )
    service_scope = os.environ.get("GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS", "")
    restricted = {
        _id(value, "restricted customer", True)
        for value in service_scope.split(",")
        if value.strip()
    }
    if restricted and customer_id not in restricted:
        raise ToolError(
            "This customer is outside the deployment's account restriction."
        )
    fingerprint = hashlib.sha256(token.token.encode()).hexdigest()
    key = hmac.digest(secret.encode(), _DOMAIN, "sha256")
    return fingerprint, key, manager


def _keywords(values: list[NegativeKeyword]) -> tuple[list[dict], int]:
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ToolError("Supply between 1 and 100 negative keywords.")
    unique = {}
    for value in values:
        try:
            item = NegativeKeyword.model_validate(value)
        except Exception as exc:
            raise ToolError(
                "Each keyword needs bare text and an explicit EXACT, PHRASE, or BROAD match_type."
            ) from exc
        text = " ".join(item.text.split())
        if (
            not text
            or len(text) > 80
            or len(text.encode("utf-8")) > 255
            or len(text.split()) > 10
            or any(unicodedata.category(c).startswith("C") for c in item.text)
            or text[0] in '["'
            or text[-1] in ']"'
        ):
            raise ToolError(
                "Use bare keyword text, without match brackets or quotes, within Google's 80-character, 255-byte and 10-word limits."
            )
        normalized = {"text": text, "match_type": item.match_type}
        unique.setdefault(_keyword_key(normalized), normalized)
    return list(unique.values()), len(values) - len(unique)


def _keyword_key(item: dict) -> tuple[str, str]:
    return (" ".join(item["text"].split()).casefold(), item["match_type"])


def _rows(client: Any, customer: str, query: str) -> list:
    try:
        response = client.get_service("GoogleAdsService").search_stream(
            customer_id=customer, query=query, timeout=30
        )
        return [row for batch in response for row in batch.results]
    except Exception as exc:
        raise ToolError(
            "Google Ads could not read the account or target. No automatic write retry was made."
        ) from exc


def _target(
    client: Any,
    customer: str,
    campaign: str,
    ad_group: str | None,
    manager: str,
) -> dict:
    hierarchy = _rows(
        client,
        manager,
        f"SELECT customer_client.id, customer_client.level, customer_client.manager, customer_client.status FROM customer_client WHERE customer_client.id = {customer} LIMIT 1",
    )
    if not hierarchy or not any(
        str(row.customer_client.id) == customer
        and row.customer_client.level > 0
        and not row.customer_client.manager
        and row.customer_client.status.name == "ENABLED"
        for row in hierarchy
    ):
        raise ToolError(
            "The customer is not an active client under the connected manager account."
        )
    accounts = _rows(
        client,
        customer,
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager, customer.status FROM customer LIMIT 1",
    )
    campaigns = _rows(
        client,
        customer,
        f"SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type FROM campaign WHERE campaign.id = {campaign} LIMIT 1",
    )
    if len(accounts) != 1 or len(campaigns) != 1:
        raise ToolError("The customer or campaign was not found.")
    account, camp = accounts[0].customer, campaigns[0].campaign
    channel = camp.advertising_channel_type.name
    supported = (
        {"SEARCH"} if ad_group else {"SEARCH", "SHOPPING", "PERFORMANCE_MAX"}
    )
    if channel not in supported:
        raise ToolError(
            "This action supports Search ad groups and Search, Shopping, or Performance Max campaigns. Other campaign types have different negative-keyword behavior."
        )
    if (
        str(account.id) != customer
        or account.manager
        or account.status.name != "ENABLED"
        or str(camp.id) != campaign
        or camp.status.name not in {"ENABLED", "PAUSED"}
    ):
        raise ToolError(
            "The account or campaign is not an active, eligible target."
        )
    result = {
        "customer_id": customer,
        "account_name": account.descriptive_name,
        "currency_code": account.currency_code,
        "time_zone": account.time_zone,
        "campaign_id": campaign,
        "campaign_name": camp.name,
        "campaign_status": camp.status.name,
        "campaign_type": camp.advertising_channel_type.name,
        "level": "ad_group" if ad_group else "campaign",
        "ad_group_id": ad_group,
    }
    if ad_group:
        groups = _rows(
            client,
            customer,
            f"SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id FROM ad_group WHERE ad_group.id = {ad_group} LIMIT 1",
        )
        if len(groups) != 1:
            raise ToolError("The ad group was not found.")
        row = groups[0]
        if (
            str(row.ad_group.id) != ad_group
            or str(row.campaign.id) != campaign
            or row.ad_group.status.name not in {"ENABLED", "PAUSED"}
        ):
            raise ToolError(
                "The ad group is not active in the selected campaign."
            )
        result.update(
            ad_group_name=row.ad_group.name,
            ad_group_status=row.ad_group.status.name,
        )
    return result


def _existing(client: Any, target: dict) -> dict:
    level = target["level"]
    resource = f"{level}_criterion"
    target_id = target[f"{level}_id"]
    query = f"SELECT {resource}.resource_name, {resource}.keyword.text, {resource}.keyword.match_type FROM {resource} WHERE {level}.id = {target_id} AND {resource}.negative = TRUE AND {resource}.type = KEYWORD AND {resource}.status != REMOVED"
    found = {}
    for row in _rows(client, target["customer_id"], query):
        criterion = getattr(row, resource)
        keyword = {
            "text": criterion.keyword.text,
            "match_type": criterion.keyword.match_type.name,
        }
        found[_keyword_key(keyword)] = {
            **keyword,
            "resource_name": criterion.resource_name,
        }
    return found


def _mutate(
    client: Any, target: dict, keywords: list[dict], validate_only: bool
):
    ad_group = target["level"] == "ad_group"
    prefix = "AdGroup" if ad_group else "Campaign"
    request = client.get_type(f"Mutate{prefix}CriteriaRequest")
    request.customer_id = target["customer_id"]
    request.validate_only = validate_only
    request.partial_failure = False
    for keyword in keywords:
        operation = client.get_type(f"{prefix}CriterionOperation")
        criterion = operation.create
        if ad_group:
            criterion.ad_group = f"customers/{target['customer_id']}/adGroups/{target['ad_group_id']}"
        else:
            criterion.campaign = f"customers/{target['customer_id']}/campaigns/{target['campaign_id']}"
        criterion.negative = True
        criterion.keyword.text = keyword["text"]
        criterion.keyword.match_type = keyword["match_type"]
        request.operations.append(operation)
    service = client.get_service(f"{prefix}CriterionService")
    method = (
        service.mutate_ad_group_criteria
        if ad_group
        else service.mutate_campaign_criteria
    )
    return method(request=request, retry=None, timeout=30)


def _encode(payload: dict, key: bytes) -> str:
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        key, _DOMAIN + body.encode(), hashlib.sha256
    ).hexdigest()
    return body + "." + signature


def _decode(token: str) -> tuple[str, str, dict]:
    try:
        if not isinstance(token, str) or len(token) > 65536:
            raise ValueError()
        body, signature = token.split(".")
        payload = json.loads(
            base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        )
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError()
        return body, signature, payload
    except Exception as exc:
        raise ToolError(
            "Invalid preview. Generate a new preview before approval."
        ) from exc


def preview_add_negative_keywords(
    customer_id: str,
    campaign_id: str,
    keywords: list[NegativeKeyword],
    ad_group_id: str | None = None,
) -> dict:
    """Preview direct campaign or ad-group negative keyword additions.

    Supports Search ad groups and Search, Shopping, or Performance Max campaigns.
    Read and show the exact account, campaign, optional ad group, text and match
    types. Skip duplicate inputs and existing direct negatives. Validate pending
    additions with Google without writing. Return a ten-minute caller-bound
    preview bound to the current Google access token. OAuth refresh can require
    a new preview. Show the user its additions and destination and obtain explicit
    approval before calling apply_add_negative_keywords. Shared/account-level
    negative lists are not changed or exhaustively checked by this tool.
    """
    customer = _id(customer_id, "customer_id", True)
    campaign = _id(campaign_id, "campaign_id")
    group = _id(ad_group_id, "ad_group_id") if ad_group_id is not None else None
    normalized, duplicate_count = _keywords(keywords)
    caller, key, manager = _context(customer)
    client = utils.get_googleads_client()
    target = _target(client, customer, campaign, group, manager)
    existing = _existing(client, target)
    additions = [
        item for item in normalized if _keyword_key(item) not in existing
    ]
    skipped = [
        existing[_keyword_key(item)]
        for item in normalized
        if _keyword_key(item) in existing
    ]
    if additions:
        try:
            _mutate(client, target, additions, validate_only=True)
        except Exception as exc:
            raise ToolError(
                "Google Ads rejected validation. No changes were made; fix the request and preview again."
            ) from exc
    now = int(time.time())
    payload = {
        "version": 1,
        "preview_id": str(uuid.uuid4()),
        "issued_at": now,
        "expires_at": now + _TTL,
        "caller": caller,
        "manager": manager,
        "target": target,
        "additions": additions,
    }
    return {
        "status": "validated",
        "target": target,
        "additions": additions,
        "already_present": skipped,
        "duplicates_removed": duplicate_count,
        "expires_at": payload["expires_at"],
        "preview_id": payload["preview_id"],
        "preview_token": _encode(payload, key),
        "google_ads_changes_made": False,
        "approval_required": True,
    }


def apply_add_negative_keywords(
    preview_token: str, approved: bool = False
) -> dict:
    """Apply only the exact additions in an explicitly approved preview.

    Call only after the user has approved the displayed account, destination,
    keyword texts and match types. Set approved=true only for that approval;
    this flag records the host's approval assertion, not independent proof of
    human identity. Requires the same Google access token and a fresh preview.
    Recheck live access, target and direct negatives. Never retry an uncertain
    write automatically; report verified readback and uncertainty accurately.
    """
    if approved is not True:
        raise ToolError(
            "Explicit approval of the displayed preview is required."
        )
    body, signature, payload = _decode(preview_token)
    try:
        customer = _id(payload["target"]["customer_id"], "customer_id", True)
        caller, key, manager = _context(customer)
        expected = hmac.new(
            key, _DOMAIN + body.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ToolError("The preview signature is invalid. Preview again.")
        now = int(time.time())
        if payload["caller"] != caller or payload["manager"] != manager:
            raise ToolError(
                "This preview belongs to a different caller or manager."
            )
        if (
            not payload["issued_at"] <= now < payload["expires_at"]
            or payload["expires_at"] - payload["issued_at"] != _TTL
        ):
            raise ToolError(
                "The preview has expired. Generate and approve a new preview."
            )
        target = payload["target"]
        additions = payload["additions"]
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("Invalid preview. Generate a new preview.") from exc
    client = utils.get_googleads_client()
    live = _target(
        client, customer, target["campaign_id"], target["ad_group_id"], manager
    )
    if live != target:
        raise ToolError(
            "The destination changed since preview. Generate and approve a new preview."
        )
    existing = _existing(client, live)
    pending = [item for item in additions if _keyword_key(item) not in existing]
    if not pending:
        return {
            "status": "already_present",
            "target": live,
            "verified": True,
            "google_ads_changes_made": False,
            "preview_id": payload["preview_id"],
            "verified_keywords": [
                existing[_keyword_key(item)] for item in additions
            ],
        }
    try:
        _mutate(client, live, pending, validate_only=True)
    except Exception as exc:
        raise ToolError(
            "Google Ads rejected revalidation. No changes were made by this call."
        ) from exc
    response = None
    mutation_error = False
    try:
        response = _mutate(client, live, pending, validate_only=False)
    except Exception:
        mutation_error = True
    try:
        observed = _existing(client, live)
    except ToolError:
        observed = {}
    verified = all(_keyword_key(item) in observed for item in additions)
    returned = (
        {item.resource_name for item in response.results}
        if response is not None
        else set()
    )
    confirmed = (
        not mutation_error
        and len(returned) == len(pending)
        and returned
        == {
            observed[_keyword_key(item)]["resource_name"]
            for item in pending
            if _keyword_key(item) in observed
        }
    )
    status = (
        "applied"
        if verified and confirmed
        else "observed_present" if verified else "uncertain"
    )
    result = {
        "status": status,
        "target": live,
        "verified": verified,
        "google_ads_changes_made": True if confirmed else None,
        "preview_id": payload["preview_id"],
        "verified_keywords": [
            observed[_keyword_key(item)]
            for item in additions
            if _keyword_key(item) in observed
        ],
        "missing_keywords": [
            item for item in additions if _keyword_key(item) not in observed
        ],
        "automatic_retry_performed": False,
    }
    utils.logger.info(
        "negative_keyword_apply %s",
        json.dumps(
            {
                "preview_id": payload["preview_id"],
                "customer_id": customer,
                "campaign_id": target["campaign_id"],
                "ad_group_id": target["ad_group_id"],
                "requested_count": len(pending),
                "status": status,
            }
        ),
    )
    return result


negatives_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True)
)(preview_add_negative_keywords)
negatives_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)(apply_add_negative_keywords)
