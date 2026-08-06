"""Portal-only tools for publishing reviewed Google Ads recommendations.

This module never mutates Google Ads. It sends a validated recommendation to
one fixed Recommendation Center configured by the service operator.
"""

import json
import os
import re
from typing import Any, Dict, List
from urllib import error, parse, request

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

recommendations_mcp = FastMCP("recommendations")

_RECOMMENDATION_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,79}$")
_CUSTOMER_ID_PATTERN = re.compile(r"^\d{10}$")
_ALLOWED_PRIORITIES = {"High", "Medium", "Low"}
_MAX_LIST_ITEMS = 12
_MAX_ITEM_LENGTH = 1_000


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ToolError(
            f"Recommendation publishing is not configured: {name} is missing."
        )
    return value


def _validate_text(name: str, value: str, maximum: int = 5_000) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{name} must be text.")
    cleaned = value.strip()
    if not cleaned:
        raise ToolError(f"{name} is required.")
    if len(cleaned) > maximum:
        raise ToolError(f"{name} exceeds the {maximum}-character limit.")
    return cleaned


def _validate_list(name: str, values: List[str]) -> List[str]:
    if not values or len(values) > _MAX_LIST_ITEMS:
        raise ToolError(f"{name} must contain 1 to {_MAX_LIST_ITEMS} items.")
    return [
        _validate_text(f"{name} item", value, _MAX_ITEM_LENGTH)
        for value in values
    ]


def _allowed_customer_ids() -> set[str]:
    raw = _required_env("RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS")
    return {value.strip() for value in raw.split(",") if value.strip()}


def _destination() -> str:
    base_url = _required_env("RECOMMENDATION_CENTER_URL").rstrip("/")
    parsed = parse.urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ToolError(
            "RECOMMENDATION_CENTER_URL must be an HTTPS origin without query parameters."
        )
    return f"{base_url}/api/recommendations"


@recommendations_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def publish_recommendation(
    recommendation_id: str,
    customer_id: str,
    account: str,
    campaign: str,
    recommendation_type: str,
    priority: str,
    confidence: int,
    title: str,
    summary: str,
    current_state: str,
    proposed_change: str,
    expected_impact: str,
    evidence: List[str],
    pros: List[str],
    tradeoffs: List[str],
    validation_criteria: List[str],
    rollback_triggers: List[str],
    rollback_instructions: str,
    evidence_captured_at: str,
) -> Dict[str, Any]:
    """Publishes one evidence-backed recommendation for human review.

    This action writes only to the Google Ads Recommendation Center. It does
    not approve the recommendation and cannot modify Google Ads. Use it only
    after completing a read-only analysis. Report a recommendation as
    published only when this tool returns ``published: true`` and the matching
    recommendation id.

    Args:
        recommendation_id: Stable idempotency key, for example REC-20260807-BMW-001.
        customer_id: Ten-digit Google Ads customer id without punctuation.
        account: Human-readable Google Ads account name.
        campaign: Campaign or campaign-group label affected by the proposal.
        recommendation_type: Category such as Conversion goals or Budget.
        priority: High, Medium, or Low.
        confidence: Integer confidence score from 0 through 100.
        title: Concise recommendation title.
        summary: Evidence-grounded explanation of the issue.
        current_state: Exact current configuration observed in Google Ads.
        proposed_change: Exact proposed configuration; never an instruction to execute it.
        expected_impact: Concise expected effect if later approved and executed.
        evidence: Observed facts supporting the recommendation.
        pros: Expected benefits.
        tradeoffs: Risks, limitations, and measurement caveats.
        validation_criteria: Post-change checks that would establish success.
        rollback_triggers: Conditions that would justify reversing the change.
        rollback_instructions: Exact reversible rollback plan.
        evidence_captured_at: ISO-8601 timestamp for the underlying evidence.
    """
    recommendation_id = _validate_text(
        "recommendation_id", recommendation_id, 80
    )
    if not _RECOMMENDATION_ID_PATTERN.fullmatch(recommendation_id):
        raise ToolError(
            "recommendation_id may contain only uppercase letters, digits, underscores, and hyphens."
        )

    customer_id = customer_id.replace("-", "").strip()
    if not _CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        raise ToolError("customer_id must contain exactly 10 digits.")
    if customer_id not in _allowed_customer_ids():
        raise ToolError(
            "This customer id is not authorized for recommendation publishing."
        )

    if priority not in _ALLOWED_PRIORITIES:
        raise ToolError("priority must be High, Medium, or Low.")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int)
        or not 0 <= confidence <= 100
    ):
        raise ToolError("confidence must be an integer from 0 through 100.")

    payload = {
        "id": recommendation_id,
        "customerId": customer_id,
        "account": _validate_text("account", account),
        "campaign": _validate_text("campaign", campaign),
        "type": _validate_text("recommendation_type", recommendation_type),
        "priority": priority,
        "confidence": confidence,
        "title": _validate_text("title", title),
        "summary": _validate_text("summary", summary),
        "current": _validate_text("current_state", current_state),
        "proposed": _validate_text("proposed_change", proposed_change),
        "impact": _validate_text("expected_impact", expected_impact),
        "evidence": _validate_list("evidence", evidence),
        "pros": _validate_list("pros", pros),
        "cons": _validate_list("tradeoffs", tradeoffs),
        "validationCriteria": _validate_list(
            "validation_criteria", validation_criteria
        ),
        "rollbackTriggers": _validate_list(
            "rollback_triggers", rollback_triggers
        ),
        "rollback": _validate_text(
            "rollback_instructions", rollback_instructions
        ),
        "evidenceCapturedAt": _validate_text(
            "evidence_captured_at", evidence_captured_at, 64
        ),
    }

    headers = {
        "Authorization": f"Bearer {_required_env('RECOMMENDATION_CENTER_INGESTION_KEY')}",
        "Content-Type": "application/json",
        "OAI-Sites-Authorization": f"Bearer {_required_env('RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN')}",
        "User-Agent": "constellation-google-ads-mcp/1.0",
    }
    http_request = request.Request(
        _destination(),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=15) as response:
            status = response.status
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise ToolError(
            f"Recommendation Center rejected the publication request with HTTP {exc.code}."
        ) from exc
    except error.URLError as exc:
        raise ToolError("Recommendation Center could not be reached.") from exc

    if status not in (200, 201):
        raise ToolError(
            f"Recommendation Center returned an unexpected HTTP {status} response."
        )

    try:
        result = json.loads(response_body)
        saved = result["recommendation"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ToolError(
            "Recommendation Center did not return a valid publication confirmation."
        ) from exc

    if saved.get("id") != recommendation_id:
        raise ToolError(
            "Recommendation Center confirmed a different recommendation id."
        )

    return {
        "published": True,
        "recommendation_id": recommendation_id,
        "status": saved.get("status", "pending_review"),
        "destination": "Google Ads Recommendation Center",
        "google_ads_changes_made": False,
    }
