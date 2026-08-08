"""Portal-only tools for publishing reviewed Google Ads recommendations.

This module never mutates Google Ads. It sends a validated recommendation to
one fixed Recommendation Center configured by the service operator.
"""

import json
import os
from datetime import date, datetime
import re
from typing import Any, Dict, List, Literal
from urllib import error, parse, request

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations
from typing_extensions import TypedDict

import ads_mcp.utils as utils

recommendations_mcp = FastMCP("recommendations")

_RECOMMENDATION_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,79}$")
_CUSTOMER_ID_PATTERN = re.compile(r"^\d{10}$")
_ALLOWED_PRIORITIES = {"High", "Medium", "Low"}
_MAX_LIST_ITEMS = 12
_MAX_ITEM_LENGTH = 1_000
_SCORECARD_PERIOD_KEYS = {
    "mtd",
    "yesterday",
    "last_7_days",
    "last_month",
    "two_months_ago",
    "mtd_last_year",
}
_SEMANTIC_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:_-]{1,159}$")
_RESOURCE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9/_:-]{1,179}$")
_COVERAGE_AREA_LABELS = {
    "conversion_tracking": "Conversion goals and tracking",
    "budget_pacing": "Budget and pacing",
    "bidding": "Bidding strategies and targets",
    "delivery": "Delivery and learning limitations",
    "campaign_structure": "Campaign structure and status",
    "ads_assets": "Ads, assets, and disapprovals",
    "targeting_traffic": "Targeting and traffic quality",
    "search_terms": "Search terms where applicable",
    "landing_pages": "Landing-page performance signals",
    "recent_changes": "Recent material account changes",
}
_COVERAGE_STATUSES = {
    "reviewed",
    "recommendation_found",
    "not_applicable",
    "unable_to_verify",
}


class AnalysisCoverageItem(TypedDict):
    """One explicit result from the ten-area account review."""

    area: Literal[
        "conversion_tracking",
        "budget_pacing",
        "bidding",
        "delivery",
        "campaign_structure",
        "ads_assets",
        "targeting_traffic",
        "search_terms",
        "landing_pages",
        "recent_changes",
    ]
    status: Literal[
        "reviewed",
        "recommendation_found",
        "not_applicable",
        "unable_to_verify",
    ]
    note: str


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
    if not isinstance(values, list) or not values or len(values) > _MAX_LIST_ITEMS:
        raise ToolError(f"{name} must contain 1 to {_MAX_LIST_ITEMS} items.")
    return [_validate_text(f"{name} item", value, _MAX_ITEM_LENGTH) for value in values]


def _validate_iso_date(name: str, value: str) -> str:
    cleaned = _validate_text(name, value, 10)
    try:
        date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ToolError(f"{name} must be a valid YYYY-MM-DD date.") from exc
    return cleaned


def _validate_iso_timestamp(name: str, value: str) -> str:
    cleaned = _validate_text(name, value, 64)
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"{name} must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError(f"{name} must include a UTC offset or Z suffix.")
    return cleaned


def _validate_run_id(value: str) -> str:
    run_id = _validate_text("run_id", value, 100)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{5,99}", run_id):
        raise ToolError(
            "run_id may contain only uppercase letters, digits, underscores, and hyphens."
        )
    return run_id


def _validate_semantic_identity(
    rule_key: str | None,
    affected_resource_keys: List[str] | None,
    condition_key: str | None,
    proposed_state_key: str | None,
) -> Dict[str, Any] | None:
    supplied = (
        rule_key is not None,
        affected_resource_keys is not None,
        condition_key is not None,
        proposed_state_key is not None,
    )
    if not any(supplied):
        return None
    if not all(supplied):
        raise ToolError(
            "Semantic identity requires rule_key, affected_resource_keys, "
            "condition_key, and proposed_state_key together."
        )

    def semantic_key(name: str, value: str) -> str:
        normalized = _validate_text(name, value, 160).lower()
        if not _SEMANTIC_KEY_PATTERN.fullmatch(normalized):
            raise ToolError(
                f"{name} must use lowercase letters, digits, colons, underscores, or hyphens."
            )
        return normalized

    if (
        not isinstance(affected_resource_keys, list)
        or not 1 <= len(affected_resource_keys) <= 50
    ):
        raise ToolError("affected_resource_keys must contain 1 to 50 items.")
    resource_keys = []
    for value in affected_resource_keys:
        normalized = _validate_text("affected_resource_keys item", value, 180).lower()
        if not _RESOURCE_KEY_PATTERN.fullmatch(normalized):
            raise ToolError(
                "affected_resource_keys items must use lowercase letters, digits, "
                "slashes, colons, underscores, or hyphens."
            )
        resource_keys.append(normalized)
    if len(resource_keys) != len(set(resource_keys)):
        raise ToolError("affected_resource_keys items must be unique.")

    return {
        "version": 1,
        "ruleKey": semantic_key("rule_key", rule_key),
        "resourceKeys": sorted(set(resource_keys)),
        "conditionKey": semantic_key("condition_key", condition_key),
        "proposedStateKey": semantic_key("proposed_state_key", proposed_state_key),
    }


def _validate_analysis_coverage(
    coverage: List[AnalysisCoverageItem] | None,
    require_complete: bool,
) -> List[Dict[str, str]]:
    if coverage is None:
        if require_complete:
            raise ToolError(
                "coverage must report all 10 analysis areas for a completed run."
            )
        return []
    if not isinstance(coverage, list) or len(coverage) > len(_COVERAGE_AREA_LABELS):
        raise ToolError("coverage must contain no more than 10 analysis areas.")

    normalized = {}
    seen = set()
    for item in coverage:
        if not isinstance(item, dict):
            raise ToolError("Each coverage item must be an object.")
        if set(item) != {"area", "status", "note"}:
            raise ToolError(
                "Each coverage item must contain only area, status, and note."
            )
        area = item.get("area")
        if area not in _COVERAGE_AREA_LABELS or area in seen:
            raise ToolError(
                "coverage area keys must be unique and use the documented 10 areas."
            )
        seen.add(area)
        coverage_status = item.get("status")
        if coverage_status not in _COVERAGE_STATUSES:
            raise ToolError(
                "coverage status must be reviewed, recommendation_found, "
                "not_applicable, or unable_to_verify."
            )
        normalized[area] = {
            "area": _COVERAGE_AREA_LABELS[area],
            "status": coverage_status,
            "note": _validate_text("coverage note", item.get("note"), 500),
        }

    if require_complete and seen != set(_COVERAGE_AREA_LABELS):
        missing = sorted(set(_COVERAGE_AREA_LABELS) - seen)
        raise ToolError(
            "coverage must report all 10 analysis areas; missing: " + ", ".join(missing)
        )
    return [normalized[area] for area in _COVERAGE_AREA_LABELS if area in normalized]


def _validate_scorecard_periods(
    periods: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(periods, list) or len(periods) != len(_SCORECARD_PERIOD_KEYS):
        raise ToolError(
            "periods must contain exactly the six required scorecard windows."
        )

    normalized = []
    seen = set()
    for period in periods:
        if not isinstance(period, dict):
            raise ToolError("Each scorecard period must be an object.")
        key = period.get("key")
        if key not in _SCORECARD_PERIOD_KEYS or key in seen:
            raise ToolError(
                "Scorecard period keys must be unique and use the required windows."
            )
        seen.add(key)
        start_date = _validate_iso_date(f"{key} start_date", period.get("start_date"))
        end_date = _validate_iso_date(f"{key} end_date", period.get("end_date"))
        if start_date > end_date:
            raise ToolError(f"{key} start_date cannot follow end_date.")

        numeric = {}
        for field in ("cost_micros", "impressions", "clicks"):
            value = period.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ToolError(f"{key} {field} must be a non-negative integer.")
            numeric[field] = value
        conversions = period.get("conversions")
        if (
            isinstance(conversions, bool)
            or not isinstance(conversions, (int, float))
            or conversions < 0
        ):
            raise ToolError(f"{key} conversions must be a non-negative number.")

        normalized.append(
            {
                "key": key,
                "startDate": start_date,
                "endDate": end_date,
                "costMicros": numeric["cost_micros"],
                "impressions": numeric["impressions"],
                "clicks": numeric["clicks"],
                "conversions": float(conversions),
            }
        )

    if seen != _SCORECARD_PERIOD_KEYS:
        raise ToolError("All six required scorecard period keys are required.")
    return normalized


def _normalize_customer_id(name: str, value: str) -> str:
    customer_id = value.replace("-", "").strip()
    if not _CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        raise ToolError(f"{name} must contain exactly 10 digits.")
    return customer_id


def _allowed_customer_ids() -> set[str] | None:
    """Returns the optional static restriction layered over MCC access."""
    raw = os.environ.get("RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS", "").strip()
    if not raw:
        return None

    return {
        _normalize_customer_id(
            "RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS entry", value
        )
        for value in raw.split(",")
        if value.strip()
    }


def _authorize_customer(customer_id: str) -> None:
    """Fails closed unless customer_id is an enabled client beneath the MCC."""
    manager_customer_id = _normalize_customer_id(
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        _required_env("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
    )
    if customer_id == manager_customer_id:
        raise ToolError(
            "The login manager account cannot receive client recommendations."
        )

    allowed_customer_ids = _allowed_customer_ids()
    if allowed_customer_ids is not None and customer_id not in allowed_customer_ids:
        raise ToolError(
            "This customer id is outside the configured publishing restriction."
        )

    # CustomerClient contains both direct and indirect descendants of a manager.
    # Filtering by the exact client id makes this a bounded, read-only proof that
    # the requested account is currently active beneath the configured MCC.
    query = f"""
        SELECT
          customer_client.id,
          customer_client.level,
          customer_client.manager,
          customer_client.status
        FROM customer_client
        WHERE customer_client.id = {customer_id}
        LIMIT 1
        PARAMETERS omit_unselected_resource_names=true
    """

    try:
        google_ads_service = utils.get_googleads_service("GoogleAdsService")
        response = google_ads_service.search_stream(
            customer_id=manager_customer_id, query=query
        )
        for batch in response:
            for row in batch.results:
                client = row.customer_client
                status = getattr(client.status, "name", str(client.status))
                if (
                    str(client.id) == customer_id
                    and client.level > 0
                    and not client.manager
                    and status == "ENABLED"
                ):
                    return
    except GoogleAdsException as exc:
        raise ToolError(
            "Google Ads could not verify this customer beneath the configured MCC."
        ) from exc
    except Exception as exc:
        raise ToolError(
            "Google Ads customer authorization could not be completed."
        ) from exc

    raise ToolError(
        "This customer id is not an enabled client beneath the configured MCC."
    )


def _destination() -> str:
    return _portal_url("/api/recommendations")


def _portal_url(path: str, query: Dict[str, Any] | None = None) -> str:
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
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{parse.urlencode(query)}"
    return url


def _portal_request(
    method: str,
    path: str,
    payload: Dict[str, Any] | None = None,
    query: Dict[str, Any] | None = None,
) -> tuple[int, Dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {_required_env('RECOMMENDATION_CENTER_INGESTION_KEY')}",
        "OAI-Sites-Authorization": f"Bearer {_required_env('RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN')}",
        "User-Agent": "constellation-google-ads-mcp/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        _portal_url(path, query), data=data, headers=headers, method=method
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            status = response.status
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise ToolError(
            f"Recommendation Center rejected the request with HTTP {exc.code}."
        ) from exc
    except error.URLError as exc:
        raise ToolError("Recommendation Center could not be reached.") from exc

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise ToolError(
            "Recommendation Center did not return a valid response."
        ) from exc
    if not isinstance(result, dict):
        raise ToolError("Recommendation Center returned an invalid response.")
    return status, result


@recommendations_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def sync_customer_catalog() -> Dict[str, Any]:
    """Synchronizes eligible MCC client accounts to the Recommendation Center.

    This action performs one read-only Google Ads hierarchy query and updates
    only the portal's account catalog. It never enrolls an account, changes an
    existing enrollment, or modifies Google Ads.
    """
    manager_customer_id = _normalize_customer_id(
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        _required_env("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
    )
    query = """
        SELECT
          customer_client.id,
          customer_client.descriptive_name,
          customer_client.level,
          customer_client.manager,
          customer_client.status,
          customer_client.currency_code,
          customer_client.time_zone
        FROM customer_client
        WHERE customer_client.level > 0
          AND customer_client.manager = FALSE
          AND customer_client.status = ENABLED
        PARAMETERS omit_unselected_resource_names=true
    """
    accounts = []
    try:
        google_ads_service = utils.get_googleads_service("GoogleAdsService")
        response = google_ads_service.search_stream(
            customer_id=manager_customer_id, query=query
        )
        for batch in response:
            for row in batch.results:
                client = row.customer_client
                status = getattr(client.status, "name", str(client.status))
                if client.level <= 0 or client.manager or status != "ENABLED":
                    continue
                accounts.append(
                    {
                        "customerId": str(client.id),
                        "descriptiveName": client.descriptive_name
                        or f"Customer {client.id}",
                        "level": client.level,
                        "status": status,
                        "currencyCode": client.currency_code or None,
                        "timeZone": client.time_zone or None,
                    }
                )
    except GoogleAdsException as exc:
        raise ToolError(
            "Google Ads could not return the configured MCC hierarchy."
        ) from exc
    except Exception as exc:
        raise ToolError(
            "Google Ads account catalog synchronization could not be completed."
        ) from exc

    if not accounts:
        raise ToolError("No eligible client accounts were found beneath the MCC.")
    status, result = _portal_request(
        "POST", "/api/accounts/catalog", {"accounts": accounts}
    )
    if status not in (200, 201) or result.get("synced") != len(accounts):
        raise ToolError(
            "Recommendation Center did not confirm the complete account catalog."
        )
    return {
        "synced": len(accounts),
        "synced_at": result.get("syncedAt"),
        "manager_customer_id": manager_customer_id,
        "google_ads_changes_made": False,
        "enrollments_changed": False,
    }


@recommendations_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def get_due_enrollments(limit: int = 3) -> Dict[str, Any]:
    """Returns the next due accounts enrolled for recommendation analysis.

    Args:
        limit: Bounded number of due accounts to return, from 1 through 10.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ToolError("limit must be an integer from 1 through 10.")
    status, result = _portal_request("GET", "/api/accounts/due", query={"limit": limit})
    accounts = result.get("accounts")
    if status != 200 or not isinstance(accounts, list):
        raise ToolError(
            "Recommendation Center did not return a valid enrollment queue."
        )
    return {
        "accounts": accounts,
        "checked_at": result.get("checkedAt"),
        "limit": limit,
        "google_ads_changes_made": False,
    }


@recommendations_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def record_enrollment_run(
    run_id: str,
    customer_id: str,
    status: str,
    recommendation_count: int,
    note: str,
    completed_at: str,
    data_through_date: str | None = None,
    coverage: List[AnalysisCoverageItem] | None = None,
) -> Dict[str, Any]:
    """Records one enrolled account analysis result in the portal.

    This action updates scheduling history only. It does not publish a
    recommendation, approve a recommendation, or modify Google Ads.

    Args:
        run_id: Stable idempotency key such as RUN-20260808-4357201747.
        customer_id: Ten-digit Google Ads customer id without punctuation.
        status: succeeded, failed, or no_recommendation.
        recommendation_count: Number of confirmed new or reopened review cards.
        note: Concise evidence or failure summary for the run history.
        completed_at: ISO-8601 completion timestamp.
        data_through_date: Most recent complete account-local day, YYYY-MM-DD.
            Required for succeeded and no_recommendation runs.
        coverage: For succeeded and no_recommendation runs, exactly one object
            for each area key: conversion_tracking, budget_pacing, bidding,
            delivery, campaign_structure, ads_assets, targeting_traffic,
            search_terms, landing_pages, and recent_changes. Each object must
            contain area, status, and an evidence-grounded note. Status must be
            reviewed, recommendation_found, not_applicable, or
            unable_to_verify. Failed runs may omit coverage.
    """
    run_id = _validate_run_id(run_id)
    customer_id = _normalize_customer_id("customer_id", customer_id)
    if status not in {"succeeded", "failed", "no_recommendation"}:
        raise ToolError("status must be succeeded, failed, or no_recommendation.")
    if (
        isinstance(recommendation_count, bool)
        or not isinstance(recommendation_count, int)
        or not 0 <= recommendation_count <= 100
    ):
        raise ToolError("recommendation_count must be an integer from 0 through 100.")
    if status in {"failed", "no_recommendation"} and recommendation_count != 0:
        raise ToolError(
            "failed and no_recommendation runs must have recommendation_count 0."
        )
    require_complete = status in {"succeeded", "no_recommendation"}
    if require_complete and data_through_date is None:
        raise ToolError(
            "data_through_date is required for succeeded and no_recommendation runs."
        )
    if (data_through_date is None) != (coverage is None):
        raise ToolError("data_through_date and coverage must be supplied together.")
    normalized_data_through_date = (
        _validate_iso_date("data_through_date", data_through_date)
        if data_through_date is not None
        else None
    )
    normalized_coverage = _validate_analysis_coverage(
        coverage,
        require_complete=require_complete or coverage is not None,
    )
    payload = {
        "runId": run_id,
        "customerId": customer_id,
        "status": status,
        "recommendationCount": recommendation_count,
        "note": _validate_text("note", note, 1_000),
        "completedAt": _validate_iso_timestamp("completed_at", completed_at),
    }
    if normalized_data_through_date is not None:
        payload["dataThroughDate"] = normalized_data_through_date
    if coverage is not None:
        payload["coverage"] = normalized_coverage
    response_status, result = _portal_request("POST", "/api/accounts/runs", payload)
    if response_status not in (200, 201) or result.get("recorded") is not True:
        raise ToolError("Recommendation Center did not confirm the enrollment run.")
    duplicate = result.get("duplicate", False)
    if not isinstance(duplicate, bool):
        raise ToolError(
            "Recommendation Center returned an invalid enrollment run confirmation."
        )
    return {
        "recorded": True,
        "duplicate": duplicate,
        "run_id": run_id,
        "customer_id": customer_id,
        "status": status,
        "data_through_date": normalized_data_through_date,
        "coverage_area_count": len(normalized_coverage),
        "next_run_at": result.get("nextRunAt"),
        "google_ads_changes_made": False,
    }


@recommendations_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def publish_account_scorecard(
    customer_id: str,
    data_through_date: str,
    captured_at: str,
    periods: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Replaces one enrolled account's portal scorecard snapshot.

    This action writes only the latest read-only performance snapshot to the
    Recommendation Center. It intentionally replaces the prior snapshot and
    does not retain daily history. It never creates or changes anything in
    Google Ads.

    Args:
        customer_id: Ten-digit Google Ads customer id without punctuation.
        data_through_date: Most recent complete account-local day, YYYY-MM-DD.
        captured_at: ISO-8601 timestamp when the read-only metrics were captured.
        periods: Exactly six objects keyed mtd, yesterday, last_7_days,
            last_month, two_months_ago, and mtd_last_year. Each object requires
            start_date, end_date, cost_micros, impressions, clicks, and
            conversions.
    """
    customer_id = _normalize_customer_id("customer_id", customer_id)
    _authorize_customer(customer_id)
    data_through_date = _validate_iso_date("data_through_date", data_through_date)
    captured_at = _validate_iso_timestamp("captured_at", captured_at)

    payload = {
        "dataThroughDate": data_through_date,
        "capturedAt": captured_at,
        "periods": _validate_scorecard_periods(periods),
    }
    status, result = _portal_request(
        "POST", f"/api/accounts/{customer_id}/scorecard", payload
    )
    if (
        status not in (200, 201)
        or result.get("published") is not True
        or result.get("customerId") != customer_id
    ):
        raise ToolError(
            "Recommendation Center did not confirm the account scorecard snapshot."
        )
    return {
        "published": True,
        "customer_id": customer_id,
        "data_through_date": data_through_date,
        "replaced_previous_snapshot": bool(
            result.get("replacedPreviousSnapshot", False)
        ),
        "destination": "Google Ads Recommendation Center",
        "google_ads_changes_made": False,
    }


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
    run_id: str | None = None,
    rule_key: str | None = None,
    affected_resource_keys: List[str] | None = None,
    condition_key: str | None = None,
    proposed_state_key: str | None = None,
) -> Dict[str, Any]:
    """Publishes one evidence-backed recommendation for human review.

    This action writes only to the Google Ads Recommendation Center. It does
    not approve the recommendation and cannot modify Google Ads. Use it only
    after completing a read-only analysis. Report a recommendation as newly
    published only when this tool returns ``published: true`` and
    ``counts_as_new_recommendation: true``. A refreshed or suppressed duplicate
    is a confirmed successful ingestion but is not another recommendation.

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
        run_id: Stable analysis-run id shared with record_enrollment_run. This
            prevents a retried publisher call from counting as another
            observation.
        rule_key: Stable lowercase rule identifier for the underlying issue.
        affected_resource_keys: Stable lowercase resource identifiers such as
            campaign:23620289473. Order does not affect identity.
        condition_key: Stable lowercase identifier for the observed condition.
        proposed_state_key: Stable lowercase identifier for the exact proposed
            target state. Supply all four semantic identity fields together.
    """
    recommendation_id = _validate_text("recommendation_id", recommendation_id, 80)
    if not _RECOMMENDATION_ID_PATTERN.fullmatch(recommendation_id):
        raise ToolError(
            "recommendation_id may contain only uppercase letters, digits, underscores, and hyphens."
        )

    identity = _validate_semantic_identity(
        rule_key,
        affected_resource_keys,
        condition_key,
        proposed_state_key,
    )
    validated_run_id = _validate_run_id(run_id) if run_id is not None else None
    validated_evidence_captured_at = _validate_iso_timestamp(
        "evidence_captured_at", evidence_captured_at
    )

    customer_id = _normalize_customer_id("customer_id", customer_id)
    _authorize_customer(customer_id)

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
        "rollbackTriggers": _validate_list("rollback_triggers", rollback_triggers),
        "rollback": _validate_text("rollback_instructions", rollback_instructions),
        "evidenceCapturedAt": validated_evidence_captured_at,
    }
    if validated_run_id is not None:
        payload["runId"] = validated_run_id
    if identity is not None:
        payload["identity"] = identity

    status, result = _portal_request("POST", "/api/recommendations", payload)
    if status not in (200, 201):
        raise ToolError(
            f"Recommendation Center returned an unexpected HTTP {status} response."
        )

    try:
        saved = result["recommendation"]
    except (KeyError, TypeError) as exc:
        raise ToolError(
            "Recommendation Center did not return a valid publication confirmation."
        ) from exc
    if not isinstance(saved, dict):
        raise ToolError(
            "Recommendation Center did not return a valid publication confirmation."
        )

    publication = result.get("publication")
    if publication is None:
        raise ToolError(
            "Recommendation Center did not return the required publication outcome."
        )

    if not isinstance(publication, dict):
        raise ToolError(
            "Recommendation Center returned an invalid publication outcome."
        )
    canonical_id = publication.get("canonicalId")
    submitted_id = publication.get("submittedId")
    outcome = publication.get("outcome")
    counts_as_new = publication.get("countsAsNewRecommendation")
    duplicate = publication.get("duplicate")
    suppression_reason = publication.get("suppressionReason")
    saved_status = saved.get("status")
    if (
        not isinstance(canonical_id, str)
        or not _RECOMMENDATION_ID_PATTERN.fullmatch(canonical_id)
        or canonical_id != saved.get("id")
        or submitted_id != recommendation_id
        or outcome not in {"created", "refreshed", "suppressed", "reopened"}
        or not isinstance(counts_as_new, bool)
        or not isinstance(duplicate, bool)
        or result.get("published") is not True
        or result.get("googleAdsChangesMade") is not False
        or counts_as_new != (outcome in {"created", "reopened"})
        or duplicate != (outcome != "created")
        or not isinstance(saved_status, str)
        or not saved_status.strip()
        or (
            outcome == "suppressed"
            and (
                not isinstance(suppression_reason, str)
                or not suppression_reason.strip()
            )
        )
    ):
        raise ToolError(
            "Recommendation Center did not return a valid publication confirmation."
        )

    return {
        "published": True,
        "accepted": True,
        "counts_as_new_recommendation": counts_as_new,
        "publication_outcome": outcome,
        "recommendation_id": canonical_id,
        "submitted_recommendation_id": recommendation_id,
        "duplicate": duplicate,
        "suppression_reason": suppression_reason,
        "status": saved.get("status", "pending_review"),
        "destination": "Google Ads Recommendation Center",
        "google_ads_changes_made": False,
    }
