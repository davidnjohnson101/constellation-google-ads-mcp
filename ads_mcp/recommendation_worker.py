"""BMW-only OpenAI Responses API runner for the recommendation canary.

The runner exposes only the audited read and portal-publication MCP tools to the
model. It never exposes a Google Ads mutation tool and does not own a schedule;
Cloud Run Jobs or another operator invokes it explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

BMW_CUSTOMER_ID = "4357201747"
BMW_ACCOUNT_NAME = "BMW of Morristown"
BMW_CAMPAIGN_IDS = ("23599481700", "23620289473", "24095737162")

ALLOWED_TOOLS = (
    "recommendations_get_due_enrollments",
    "metadata_get_resource_metadata",
    "search_search",
    "recommendations_collect_and_publish_account_scorecard",
    "recommendations_publish_recommendation",
    "recommendations_record_enrollment_run",
)

RUNNER_PROMPT = f"""
Execute one BMW Ads recommendation canary using only the provided Constellation
Google Ads MCP tools. This request is the complete and authoritative execution
contract. There is no inherited conversation context.

SAFETY AND SCOPE

1. Call recommendations_get_due_enrollments exactly once with limit 1. If no
   account is due, stop successfully and report no_due.
2. Fail closed unless the due account is customer {BMW_CUSTOMER_ID} and its
   name contains {BMW_ACCOUNT_NAME!r}. Never substitute another account.
3. Use America/New_York and set data_through_date to the most recent complete
   account-local day. The run ID must be RUN-YYYYMMDD-{BMW_CUSTOMER_ID}, where
   YYYYMMDD is data_through_date with hyphens removed. Reuse that exact run ID
   for every recommendation and the final run record.
4. Google Ads is strictly read-only. Never create, edit, apply, approve,
   dismiss, pause, enable, upload, or remove anything. Analyze only campaigns
   {', '.join(BMW_CAMPAIGN_IDS)} when campaign-bounded evidence is required.
5. A metadata preflight is explicitly required and permitted. Call
   metadata_get_resource_metadata for every Google Ads resource before the
   first search using that resource. Do not treat any older metadata
   prohibition as applicable to this isolated run.
6. GAQL compatibility is a fail-closed preflight, not a retry strategy. Never
   select, filter, or order by metrics.conversion_last_conversion_date in a
   search that also selects, filters, or orders by segments.date. Date-window
   performance searches must omit metrics.conversion_last_conversion_date. If
   conversion recency is material to a review conclusion, use a separate
   resource-compatible search without segments.date. A field being selectable
   in metadata does not establish that it is compatible with every segment.

SCORECARD

7. After the customer metadata preflight, call
   recommendations_collect_and_publish_account_scorecard exactly once with
   customer {BMW_CUSTOMER_ID} and data_through_date. This purpose-built action
   reads customer-level daily metrics and calculates exactly these six windows
   server-side: mtd, yesterday, last_7_days, last_month, two_months_ago, and
   mtd_last_year. Do not call the free-form scorecard publisher and do not
   calculate or supply scorecard periods yourself. Require the matching
   publication confirmation and all six canonical period keys.

TEN-AREA REVIEW

8. Review exactly these areas with evidence through data_through_date:
   conversion_tracking, budget_pacing, bidding, delivery,
   campaign_structure, ads_assets, targeting_traffic, search_terms,
   landing_pages, and recent_changes. Use not_applicable or unable_to_verify
   when evidence cannot safely establish a conclusion. Never guess.
9. Publish only evidence-backed recommendations. Each publication must include
   run_id and all four semantic identity fields: rule_key,
   affected_resource_keys, condition_key, and proposed_state_key. Stable
   identity must describe the issue and affected resources, never confidence,
   prose, timestamps, or evidence values.
10. Count only publications whose exact response has
    counts_as_new_recommendation=true. Refreshed and suppressed duplicates are
    successful ingestion but contribute zero.
11. Record the enrollment run exactly once. A completed analysis requires
    data_through_date and exactly ten coverage objects. A failure after the due
    account is known must be recorded once as failed with recommendation_count
    zero and no coverage.

FINAL RESPONSE

Return concise JSON with status, customer_id, data_through_date, run_id,
scorecard_published, publication outcomes, recommendation_count,
run_recorded, and google_ads_changes_made. Never claim a portal action unless
its tool confirmation succeeded.
""".strip()


class WorkerContractError(RuntimeError):
    """Raised when a model response violates the bounded runner contract."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WorkerContractError(f"{name} is required")
    return value


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_service_jwt(now: int | None = None) -> str:
    """Creates a short-lived HS256 credential for the service MCP endpoint."""
    secret = _required_env("GOOGLE_ADS_MCP_SERVICE_JWT_SECRET")
    if len(secret) < 32:
        raise WorkerContractError(
            "GOOGLE_ADS_MCP_SERVICE_JWT_SECRET must contain at least 32 characters"
        )
    issued_at = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": os.environ.get(
            "GOOGLE_ADS_MCP_SERVICE_JWT_ISSUER", "constellation-ads-worker"
        ),
        "aud": os.environ.get(
            "GOOGLE_ADS_MCP_SERVICE_JWT_AUDIENCE",
            "constellation-google-ads-mcp",
        ),
        "sub": "bmw-recommendation-worker",
        "client_id": "bmw-recommendation-worker",
        "scope": "google-ads.read recommendation-center.write",
        "iat": issued_at,
        "nbf": issued_at - 5,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    encoded_header = _b64url(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    )
    encoded_payload = _b64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    message = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = _b64url(
        hmac.new(secret.encode(), message, hashlib.sha256).digest()
    )
    return f"{encoded_header}.{encoded_payload}.{signature}"


def _as_dict(response: Any) -> Dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    raise WorkerContractError("OpenAI response could not be inspected")


def _json_object(value: Any, label: str) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WorkerContractError(f"{label} was not valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise WorkerContractError(f"{label} was not a JSON object")


def _mcp_calls(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    output = payload.get("output")
    if not isinstance(output, list):
        raise WorkerContractError(
            "OpenAI response did not contain output items"
        )
    for item in output:
        if isinstance(item, dict) and item.get("type") == "mcp_call":
            yield item


def _contains_gaql_field(value: Any, field: str) -> bool:
    """Returns whether one MCP search argument references an exact GAQL field."""
    if isinstance(value, str):
        return (
            re.search(
                rf"(?<![A-Za-z0-9_.]){re.escape(field)}(?![A-Za-z0-9_.])", value
            )
            is not None
        )
    if isinstance(value, list):
        return any(_contains_gaql_field(item, field) for item in value)
    return False


def _validate_search_arguments(call: Dict[str, Any]) -> None:
    """Audits the date-segment compatibility rule in the authoritative call."""
    arguments = _json_object(call.get("arguments"), "search arguments")
    query_parts = [
        arguments.get("fields"),
        arguments.get("conditions"),
        arguments.get("orderings"),
    ]
    if any(
        _contains_gaql_field(part, "segments.date") for part in query_parts
    ) and any(
        _contains_gaql_field(part, "metrics.conversion_last_conversion_date")
        for part in query_parts
    ):
        raise WorkerContractError(
            "A date-segmented search included "
            "metrics.conversion_last_conversion_date; use a separate "
            "unsegmented conversion-recency search"
        )


def validate_response(response: Any) -> Dict[str, Any]:
    """Validates the authoritative MCP calls, not the model's prose summary."""
    payload = _as_dict(response)
    calls = list(_mcp_calls(payload))
    if not calls:
        raise WorkerContractError("OpenAI response made no MCP calls")
    names = [str(call.get("name", "")) for call in calls]
    disallowed = sorted(set(names) - set(ALLOWED_TOOLS))
    if disallowed:
        raise WorkerContractError(
            "OpenAI response called disallowed tools: " + ", ".join(disallowed)
        )
    if names.count("recommendations_get_due_enrollments") != 1:
        raise WorkerContractError("The due queue must be checked exactly once")
    for call in calls:
        if call.get("name") == "search_search":
            _validate_search_arguments(call)
        if call.get("error") not in (None, ""):
            raise WorkerContractError(
                f"MCP call {call.get('name')} failed: {call.get('error')}"
            )

    by_name: Dict[str, list[Dict[str, Any]]] = {}
    for call in calls:
        by_name.setdefault(str(call.get("name")), []).append(call)
    due = _json_object(
        by_name["recommendations_get_due_enrollments"][0].get("output"),
        "due queue output",
    )
    accounts = due.get("accounts")
    if accounts == []:
        if len(calls) != 1:
            raise WorkerContractError(
                "A no-due run must stop after the queue check"
            )
        return {
            "status": "no_due",
            "customer_id": None,
            "google_ads_changes_made": False,
        }
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise WorkerContractError(
            "The due queue did not return exactly one account"
        )
    account = accounts[0]
    if not isinstance(account, dict):
        raise WorkerContractError("The due account was malformed")
    customer_id = str(account.get("customerId", "")).replace("-", "")
    account_name = str(account.get("descriptiveName", ""))
    if customer_id != BMW_CUSTOMER_ID or BMW_ACCOUNT_NAME not in account_name:
        raise WorkerContractError(
            "The due account is outside the BMW canary scope"
        )

    if not by_name.get("metadata_get_resource_metadata"):
        raise WorkerContractError("The required metadata preflight did not run")
    if not by_name.get("search_search"):
        raise WorkerContractError("No Google Ads read-only searches ran")
    scorecards = by_name.get(
        "recommendations_collect_and_publish_account_scorecard", []
    )
    if len(scorecards) != 1:
        raise WorkerContractError(
            "The scorecard must be published exactly once"
        )
    scorecard = _json_object(scorecards[0].get("output"), "scorecard output")
    if (
        scorecard.get("published") is not True
        or scorecard.get("customer_id") != BMW_CUSTOMER_ID
        or scorecard.get("google_ads_changes_made") is not False
        or scorecard.get("period_keys")
        != [
            "mtd",
            "yesterday",
            "last_7_days",
            "last_month",
            "two_months_ago",
            "mtd_last_year",
        ]
    ):
        raise WorkerContractError("The scorecard publication was not confirmed")

    records = by_name.get("recommendations_record_enrollment_run", [])
    if len(records) != 1:
        raise WorkerContractError(
            "The enrollment run must be recorded exactly once"
        )
    record_call = records[0]
    record = _json_object(record_call.get("output"), "run record output")
    arguments = _json_object(
        record_call.get("arguments"), "run record arguments"
    )
    if (
        record.get("recorded") is not True
        or record.get("customer_id") != BMW_CUSTOMER_ID
        or record.get("google_ads_changes_made") is not False
    ):
        raise WorkerContractError("The enrollment run was not confirmed")
    if record.get("status") != "succeeded":
        raise WorkerContractError(
            f"The BMW analysis recorded status {record.get('status')!r}"
        )
    data_through_date = str(record.get("data_through_date", ""))
    expected_run_id = (
        f"RUN-{data_through_date.replace('-', '')}-{BMW_CUSTOMER_ID}"
    )
    if record.get("run_id") != expected_run_id:
        raise WorkerContractError("Run ID does not match data_through_date")
    if record.get("coverage_area_count") != 10:
        raise WorkerContractError(
            "The run did not record all ten coverage areas"
        )

    new_count = 0
    publication_outcomes = []
    for call in by_name.get("recommendations_publish_recommendation", []):
        result = _json_object(call.get("output"), "recommendation output")
        if (
            result.get("accepted") is not True
            or result.get("google_ads_changes_made") is not False
        ):
            raise WorkerContractError(
                "A recommendation was not safely accepted"
            )
        if result.get("counts_as_new_recommendation") is True:
            new_count += 1
        publication_outcomes.append(
            {
                "canonical_id": result.get("recommendation_id"),
                "outcome": result.get("publication_outcome"),
                "duplicate": result.get("duplicate"),
                "counts_as_new": result.get("counts_as_new_recommendation"),
            }
        )
    if arguments.get("recommendation_count") != new_count:
        raise WorkerContractError(
            "Recorded recommendation_count does not match confirmed new outcomes"
        )
    for call in calls:
        if call.get("name") in {
            "recommendations_collect_and_publish_account_scorecard",
            "recommendations_publish_recommendation",
            "recommendations_record_enrollment_run",
        }:
            result = _json_object(
                call.get("output"), f"{call.get('name')} output"
            )
            if result.get("google_ads_changes_made") is not False:
                raise WorkerContractError(
                    "A portal action lacked the no-change proof"
                )

    return {
        "status": "succeeded",
        "customer_id": BMW_CUSTOMER_ID,
        "data_through_date": data_through_date,
        "run_id": expected_run_id,
        "scorecard_published": True,
        "publication_outcomes": publication_outcomes,
        "recommendation_count": new_count,
        "run_recorded": True,
        "google_ads_changes_made": False,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def run_worker() -> Dict[str, Any]:
    from openai import OpenAI

    server_url = _required_env("GOOGLE_ADS_MCP_SERVICE_URL")
    if not server_url.startswith("https://") or not server_url.endswith("/mcp"):
        raise WorkerContractError(
            "GOOGLE_ADS_MCP_SERVICE_URL must be an HTTPS URL ending in /mcp"
        )
    client = OpenAI(
        api_key=_required_env("OPENAI_API_KEY"),
        timeout=1800.0,
        max_retries=2,
    )
    response = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6"),
        tools=[
            {
                "type": "mcp",
                "server_label": "constellation_google_ads",
                "server_description": (
                    "Strictly read-only Google Ads analysis plus controlled "
                    "Recommendation Center publication."
                ),
                "server_url": server_url,
                "authorization": create_service_jwt(),
                "require_approval": "never",
                "allowed_tools": list(ALLOWED_TOOLS),
            }
        ],
        input=RUNNER_PROMPT,
    )
    return validate_response(response)


def main() -> None:
    try:
        result = run_worker()
    except Exception as exc:
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "google_ads_changes_made": False,
        }
        print(json.dumps(failure, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
