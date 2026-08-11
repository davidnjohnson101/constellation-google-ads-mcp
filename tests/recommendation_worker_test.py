"""Unit tests for the bounded BMW Responses API runner."""

import asyncio
import base64
import json
import os
import unittest
from unittest.mock import patch

from ads_mcp.recommendation_worker import (
    BMW_CUSTOMER_ID,
    RUNNER_PROMPT,
    WorkerContractError,
    create_service_jwt,
    validate_response,
)


def call(name, output, arguments=None):
    item = {
        "type": "mcp_call",
        "name": name,
        "output": json.dumps(output),
        "error": None,
    }
    if arguments is not None:
        item["arguments"] = json.dumps(arguments)
    return item


def successful_response(recommendation_count=0):
    date = "2026-08-08"
    run_id = f"RUN-20260808-{BMW_CUSTOMER_ID}"
    output = [
        call(
            "recommendations_get_due_enrollments",
            {
                "accounts": [
                    {
                        "customerId": BMW_CUSTOMER_ID,
                        "descriptiveName": "BMW of Morristown :: Tier 3",
                    }
                ],
                "google_ads_changes_made": False,
            },
        ),
        call("metadata_get_resource_metadata", {"fields": ["customer.id"]}),
        call(
            "search_search",
            [{"segments.date": date, "metrics.conversions": 1}],
            {
                "customer_id": BMW_CUSTOMER_ID,
                "resource": "customer",
                "fields": ["segments.date", "metrics.conversions"],
                "conditions": [
                    "segments.date BETWEEN '2026-08-08' AND '2026-08-08'"
                ],
                "orderings": ["segments.date ASC"],
            },
        ),
        call(
            "recommendations_collect_and_publish_account_scorecard",
            {
                "published": True,
                "customer_id": BMW_CUSTOMER_ID,
                "data_through_date": date,
                "replaced_previous_snapshot": True,
                "period_keys": [
                    "mtd",
                    "yesterday",
                    "last_7_days",
                    "last_month",
                    "two_months_ago",
                    "mtd_last_year",
                ],
                "aggregation": "deterministic_customer_daily",
                "google_ads_changes_made": False,
            },
        ),
        call(
            "recommendations_publish_recommendation",
            {
                "published": True,
                "accepted": True,
                "counts_as_new_recommendation": False,
                "publication_outcome": "refreshed",
                "recommendation_id": "REC-20260808-BMW-001",
                "submitted_recommendation_id": "REC-20260809-BMW-001",
                "duplicate": True,
                "google_ads_changes_made": False,
            },
        ),
        call(
            "recommendations_record_enrollment_run",
            {
                "recorded": True,
                "duplicate": False,
                "run_id": run_id,
                "customer_id": BMW_CUSTOMER_ID,
                "status": "succeeded",
                "data_through_date": date,
                "coverage_area_count": 10,
                "google_ads_changes_made": False,
            },
            {"recommendation_count": recommendation_count},
        ),
    ]
    return {"output": output}


class RecommendationWorkerTest(unittest.TestCase):
    def test_runner_contract_splits_conversion_recency_from_date_segments(self):
        self.assertIn(
            "Never\n   select, filter, or order by metrics.conversion_last_conversion_date",
            RUNNER_PROMPT,
        )
        self.assertIn("use a separate", RUNNER_PROMPT)
        self.assertIn("without segments.date", RUNNER_PROMPT)

    def test_service_jwt_contains_bounded_claims(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_SERVICE_JWT_SECRET": "x" * 48},
            clear=False,
        ):
            token = create_service_jwt(now=1_786_270_400)
        parts = token.split(".")
        self.assertEqual(len(parts), 3)
        padding = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        self.assertEqual(claims["sub"], "bmw-recommendation-worker")
        self.assertEqual(claims["exp"] - claims["iat"], 900)
        self.assertIn("google-ads.read", claims["scope"])

    def test_service_jwt_is_accepted_by_fastmcp_verifier(self):
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        secret = "s" * 48
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_SERVICE_JWT_SECRET": secret},
            clear=False,
        ):
            token = create_service_jwt()
        verifier = JWTVerifier(
            public_key=secret,
            issuer="constellation-ads-worker",
            audience="constellation-google-ads-mcp",
            algorithm="HS256",
            required_scopes=[
                "google-ads.read",
                "recommendation-center.write",
            ],
        )

        access_token = asyncio.run(verifier.verify_token(token))

        self.assertIsNotNone(access_token)
        self.assertEqual(access_token.client_id, "bmw-recommendation-worker")
        self.assertIn("recommendation-center.write", access_token.scopes)

    def test_validates_refreshed_duplicate_without_counting_it(self):
        result = validate_response(successful_response())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["recommendation_count"], 0)
        self.assertEqual(result["publication_outcomes"][0]["outcome"], "refreshed")
        self.assertFalse(result["google_ads_changes_made"])

    def test_rejects_recorded_count_that_does_not_match_publications(self):
        with self.assertRaisesRegex(
            WorkerContractError, "recommendation_count does not match"
        ):
            validate_response(successful_response(recommendation_count=1))

    def test_rejects_last_conversion_date_in_date_segmented_search(self):
        response = successful_response()
        search_call = next(
            item
            for item in response["output"]
            if item["name"] == "search_search"
        )
        arguments = json.loads(search_call["arguments"])
        arguments["fields"].append("metrics.conversion_last_conversion_date")
        search_call["arguments"] = json.dumps(arguments)

        with self.assertRaisesRegex(
            WorkerContractError, "date-segmented search included"
        ):
            validate_response(response)

    def test_allows_last_conversion_date_in_separate_unsegmented_search(self):
        response = successful_response()
        search_call = next(
            item
            for item in response["output"]
            if item["name"] == "search_search"
        )
        search_call["arguments"] = json.dumps(
            {
                "customer_id": BMW_CUSTOMER_ID,
                "resource": "campaign",
                "fields": [
                    "campaign.id",
                    "metrics.conversion_last_conversion_date",
                ],
                "conditions": ["campaign.status = 'ENABLED'"],
                "orderings": [],
            }
        )

        result = validate_response(response)

        self.assertEqual(result["status"], "succeeded")

    def test_rejects_disallowed_tool(self):
        response = successful_response()
        response["output"].append(call("campaigns_mutate", {"ok": True}))
        with self.assertRaisesRegex(WorkerContractError, "disallowed tools"):
            validate_response(response)

    def test_no_due_stops_after_queue(self):
        result = validate_response(
            {
                "output": [
                    call(
                        "recommendations_get_due_enrollments",
                        {"accounts": [], "google_ads_changes_made": False},
                    )
                ]
            }
        )
        self.assertEqual(result["status"], "no_due")


if __name__ == "__main__":
    unittest.main()
