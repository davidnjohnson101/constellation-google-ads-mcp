"""Tests for Recommendation Center publishing."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools.recommendations import (
    get_due_enrollments,
    publish_account_scorecard,
    publish_recommendation,
    record_enrollment_run,
    sync_customer_catalog,
)

VALID_ARGUMENTS = {
    "recommendation_id": "REC-20260807-BMW-001",
    "customer_id": "4357201747",
    "account": "BMW of Morristown",
    "campaign": "Vehicle Ads + Demand Gen",
    "recommendation_type": "Conversion goals",
    "priority": "High",
    "confidence": 98,
    "title": "Remove VDP page views from active campaign bidding goals",
    "summary": "VDP page views dominate the active bidding signals.",
    "current_state": "Both campaigns use Macro + VDP.",
    "proposed_change": "Assign both campaigns to Macro and mark VDP actions secondary.",
    "expected_impact": "Lead-only optimization signals",
    "evidence": ["VDP views represent 91.19% of reported conversions."],
    "pros": ["Aligns bidding with lead actions."],
    "tradeoffs": ["Reported conversions will fall."],
    "validation_criteria": ["Lead conversion volume remains measurable."],
    "rollback_triggers": ["Qualified lead volume materially declines."],
    "rollback_instructions": "Reassign both campaigns to Macro + VDP.",
    "evidence_captured_at": "2026-08-07T06:00:00-04:00",
    "run_id": "RUN-20260808-4357201747",
    "rule_key": "conversion-goals:remove-vdp-bidding",
    "affected_resource_keys": [
        "campaign:24095737162",
        "campaign:23620289473",
    ],
    "condition_key": "macro-vdp-goal-active",
    "proposed_state_key": "macro-goal-only",
}

COVERAGE = [
    {
        "area": area,
        "status": (
            "recommendation_found" if area == "conversion_tracking" else "reviewed"
        ),
        "note": f"Read-only evidence checked for {area}.",
    }
    for area in (
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
    )
]

SCORECARD_ARGUMENTS = {
    "customer_id": "4357201747",
    "data_through_date": "2026-08-07",
    "captured_at": "2026-08-08T06:00:00-04:00",
    "periods": [
        {
            "key": key,
            "start_date": "2026-08-01",
            "end_date": "2026-08-07",
            "cost_micros": 1000000,
            "impressions": 1000,
            "clicks": 100,
            "conversions": 10.5,
        }
        for key in (
            "mtd",
            "yesterday",
            "last_7_days",
            "last_month",
            "two_months_ago",
            "mtd_last_year",
        )
    ],
}


class _Response:
    def __init__(self, body=None, status=201):
        self.status = status
        self.body = (
            body
            if body is not None
            else {
                "published": True,
                "googleAdsChangesMade": False,
                "recommendation": {
                    "id": "REC-20260807-BMW-001",
                    "status": "pending_review",
                },
                "publication": {
                    "outcome": "created",
                    "submittedId": "REC-20260807-BMW-001",
                    "canonicalId": "REC-20260807-BMW-001",
                    "duplicate": False,
                    "countsAsNewRecommendation": True,
                    "suppressionReason": None,
                },
            }
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.body).encode("utf-8")


def _hierarchy_response(
    customer_id="4357201747",
    level=1,
    manager=False,
    status="ENABLED",
    descriptive_name="BMW of Morristown",
    currency_code="USD",
    time_zone="America/New_York",
):
    client = SimpleNamespace(
        id=int(customer_id),
        level=level,
        manager=manager,
        status=SimpleNamespace(name=status),
        descriptive_name=descriptive_name,
        currency_code=currency_code,
        time_zone=time_zone,
    )
    row = SimpleNamespace(customer_client=client)
    return [SimpleNamespace(results=[row])]


class RecommendationPublishingTest(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS": "4357201747",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_publishes_to_fixed_portal_without_ads_write(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response()

        result = publish_recommendation(**VALID_ARGUMENTS)

        self.assertTrue(result["published"])
        self.assertFalse(result["google_ads_changes_made"])
        outgoing = mock_urlopen.call_args.args[0]
        self.assertEqual(
            outgoing.full_url,
            "https://recommendations.example/api/recommendations",
        )
        payload = json.loads(outgoing.data.decode("utf-8"))
        self.assertEqual(payload["customerId"], "4357201747")
        self.assertEqual(
            payload["validationCriteria"],
            VALID_ARGUMENTS["validation_criteria"],
        )
        self.assertEqual(
            payload["rollbackTriggers"], VALID_ARGUMENTS["rollback_triggers"]
        )
        self.assertEqual(payload["runId"], VALID_ARGUMENTS["run_id"])
        self.assertEqual(
            payload["identity"],
            {
                "version": 1,
                "ruleKey": "conversion-goals:remove-vdp-bidding",
                "resourceKeys": [
                    "campaign:23620289473",
                    "campaign:24095737162",
                ],
                "conditionKey": "macro-vdp-goal-active",
                "proposedStateKey": "macro-goal-only",
            },
        )
        search_call = mock_get_service.return_value.search_stream.call_args
        self.assertEqual(search_call.kwargs["customer_id"], "4599605095")
        self.assertIn("customer_client.id = 4357201747", search_call.kwargs["query"])

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_accepts_legacy_input_contract_with_v14_confirmation(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response()
        legacy_arguments = {
            key: value
            for key, value in VALID_ARGUMENTS.items()
            if key
            not in {
                "run_id",
                "rule_key",
                "affected_resource_keys",
                "condition_key",
                "proposed_state_key",
            }
        }

        result = publish_recommendation(**legacy_arguments)

        self.assertTrue(result["published"])
        self.assertTrue(result["counts_as_new_recommendation"])
        payload = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("identity", payload)
        self.assertNotIn("runId", payload)

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_ambiguous_response_without_publication_outcome(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response(
            {
                "published": True,
                "googleAdsChangesMade": False,
                "recommendation": {
                    "id": "REC-20260807-BMW-001",
                    "status": "pending_review",
                },
            }
        )

        with self.assertRaisesRegex(ToolError, "required publication outcome"):
            publish_recommendation(**VALID_ARGUMENTS)

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_confirms_duplicate_without_publishing_another_card(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response(
            {
                "published": True,
                "googleAdsChangesMade": False,
                "recommendation": {
                    "id": "REC-20260807-BMW-GOALS-001",
                    "status": "pending_review",
                },
                "publication": {
                    "outcome": "refreshed",
                    "submittedId": "REC-20260807-BMW-001",
                    "canonicalId": "REC-20260807-BMW-GOALS-001",
                    "duplicate": True,
                    "countsAsNewRecommendation": False,
                    "suppressionReason": None,
                },
            },
            status=200,
        )

        result = publish_recommendation(**VALID_ARGUMENTS)

        self.assertTrue(result["accepted"])
        self.assertTrue(result["published"])
        self.assertFalse(result["counts_as_new_recommendation"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["recommendation_id"], "REC-20260807-BMW-GOALS-001")
        self.assertEqual(result["publication_outcome"], "refreshed")

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_surfaces_lifecycle_suppression_without_counting_it(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response(
            {
                "published": True,
                "googleAdsChangesMade": False,
                "recommendation": {
                    "id": "REC-20260807-BMW-GOALS-001",
                    "status": "rejected",
                },
                "publication": {
                    "outcome": "suppressed",
                    "submittedId": "REC-20260808-BMW-002",
                    "canonicalId": "REC-20260807-BMW-GOALS-001",
                    "duplicate": True,
                    "countsAsNewRecommendation": False,
                    "suppressionReason": "resolved_issue_unchanged",
                },
            },
            status=200,
        )
        arguments = {
            **VALID_ARGUMENTS,
            "recommendation_id": "REC-20260808-BMW-002",
        }

        result = publish_recommendation(**arguments)

        self.assertTrue(result["published"])
        self.assertFalse(result["counts_as_new_recommendation"])
        self.assertEqual(result["publication_outcome"], "suppressed")
        self.assertEqual(result["suppression_reason"], "resolved_issue_unchanged")

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_counts_a_later_regression_reopened_for_review(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response(
            {
                "published": True,
                "googleAdsChangesMade": False,
                "recommendation": {
                    "id": "REC-20260807-BMW-GOALS-001",
                    "status": "pending_review",
                },
                "publication": {
                    "outcome": "reopened",
                    "submittedId": "REC-20260808-BMW-003",
                    "canonicalId": "REC-20260807-BMW-GOALS-001",
                    "duplicate": True,
                    "countsAsNewRecommendation": True,
                    "suppressionReason": None,
                },
            },
            status=200,
        )
        arguments = {
            **VALID_ARGUMENTS,
            "recommendation_id": "REC-20260808-BMW-003",
        }

        result = publish_recommendation(**arguments)

        self.assertTrue(result["published"])
        self.assertTrue(result["counts_as_new_recommendation"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["publication_outcome"], "reopened")

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_fails_closed_on_malformed_v14_confirmation(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        base = _Response().body
        mutations = {
            "submitted id": lambda body: body["publication"].update(
                {"submittedId": "REC-WRONG-ID"}
            ),
            "canonical id": lambda body: body["publication"].update(
                {"canonicalId": "REC-DIFFERENT-ID"}
            ),
            "string count": lambda body: body["publication"].update(
                {"countsAsNewRecommendation": "true"}
            ),
            "false publication": lambda body: body.update({"published": False}),
            "wrong duplicate flag": lambda body: body["publication"].update(
                {"duplicate": True}
            ),
            "missing outcome envelope": lambda body: body.update({"publication": {}}),
            "blank suppression reason": lambda body: body["publication"].update(
                {
                    "outcome": "suppressed",
                    "duplicate": True,
                    "countsAsNewRecommendation": False,
                    "suppressionReason": "   ",
                }
            ),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                body = json.loads(json.dumps(base))
                mutate(body)
                mock_urlopen.return_value = _Response(body, status=200)
                with self.assertRaisesRegex(
                    ToolError, "valid publication confirmation"
                ):
                    publish_recommendation(**VALID_ARGUMENTS)

    @patch.dict(
        "os.environ",
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095"},
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_partial_or_invalid_semantic_identity(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        partial = {**VALID_ARGUMENTS, "proposed_state_key": None}
        with self.assertRaisesRegex(ToolError, "Semantic identity requires"):
            publish_recommendation(**partial)

        invalid = {
            **VALID_ARGUMENTS,
            "affected_resource_keys": ["Campaign With Spaces"],
        }
        with self.assertRaisesRegex(ToolError, "affected_resource_keys"):
            publish_recommendation(**invalid)
        duplicate = {
            **VALID_ARGUMENTS,
            "affected_resource_keys": [
                "campaign:23620289473",
                "campaign:23620289473",
            ],
        }
        with self.assertRaisesRegex(ToolError, "must be unique"):
            publish_recommendation(**duplicate)
        mock_urlopen.assert_not_called()
        mock_get_service.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_invalid_run_or_timestamp_before_external_calls(
        self, mock_get_service, mock_urlopen
    ):
        with self.assertRaisesRegex(ToolError, "run_id"):
            publish_recommendation(**{**VALID_ARGUMENTS, "run_id": "bad-run"})
        with self.assertRaisesRegex(ToolError, "UTC offset"):
            publish_recommendation(
                **{
                    **VALID_ARGUMENTS,
                    "evidence_captured_at": "2026-08-08T10:00:00",
                }
            )
        mock_get_service.assert_not_called()
        mock_urlopen.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS": "4357201747",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_customer_outside_allowlist(self, mock_get_service):
        arguments = {**VALID_ARGUMENTS, "customer_id": "1111111111"}
        with self.assertRaisesRegex(ToolError, "publishing restriction"):
            publish_recommendation(**arguments)
        mock_get_service.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
            "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
            "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
        },
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_allows_indirect_enabled_client_without_allowlist(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response(
            level=3
        )
        mock_urlopen.return_value = _Response()

        result = publish_recommendation(**VALID_ARGUMENTS)

        self.assertTrue(result["published"])

    @patch.dict(
        "os.environ",
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095"},
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_customer_outside_mcc(self, mock_get_service, mock_urlopen):
        mock_get_service.return_value.search_stream.return_value = []

        with self.assertRaisesRegex(ToolError, "not an enabled client"):
            publish_recommendation(**VALID_ARGUMENTS)
        mock_urlopen.assert_not_called()

    @patch.dict(
        "os.environ",
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095"},
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_disabled_customer(self, mock_get_service, mock_urlopen):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response(
            status="CANCELED"
        )

        with self.assertRaisesRegex(ToolError, "not an enabled client"):
            publish_recommendation(**VALID_ARGUMENTS)
        mock_urlopen.assert_not_called()

    @patch.dict(
        "os.environ",
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095"},
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_manager_customer(self, mock_get_service, mock_urlopen):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response(
            manager=True
        )

        with self.assertRaisesRegex(ToolError, "not an enabled client"):
            publish_recommendation(**VALID_ARGUMENTS)
        mock_urlopen.assert_not_called()

    @patch.dict(
        "os.environ",
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095"},
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_fails_closed_when_hierarchy_lookup_errors(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.side_effect = RuntimeError(
            "network error"
        )

        with self.assertRaisesRegex(ToolError, "could not be completed"):
            publish_recommendation(**VALID_ARGUMENTS)
        mock_urlopen.assert_not_called()

    @patch.dict(
        "os.environ",
        {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4357201747"},
        clear=True,
    )
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_login_manager_itself(self, mock_get_service):
        with self.assertRaisesRegex(ToolError, "manager account"):
            publish_recommendation(**VALID_ARGUMENTS)
        mock_get_service.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    def test_fails_closed_when_configuration_is_missing(self):
        with self.assertRaisesRegex(ToolError, "is missing"):
            publish_recommendation(**VALID_ARGUMENTS)


class AccountScorecardPublishingTest(unittest.TestCase):
    portal_environment = {
        "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
        "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
        "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
    }

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_replaces_portal_snapshot_without_ads_write(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        mock_urlopen.return_value = _Response(
            {
                "published": True,
                "customerId": "4357201747",
                "replacedPreviousSnapshot": True,
            },
            status=200,
        )

        result = publish_account_scorecard(**SCORECARD_ARGUMENTS)

        self.assertTrue(result["published"])
        self.assertTrue(result["replaced_previous_snapshot"])
        self.assertFalse(result["google_ads_changes_made"])
        outgoing = mock_urlopen.call_args.args[0]
        self.assertEqual(
            outgoing.full_url,
            "https://recommendations.example/api/accounts/4357201747/scorecard",
        )
        payload = json.loads(outgoing.data.decode("utf-8"))
        self.assertEqual(payload["dataThroughDate"], "2026-08-07")
        self.assertEqual(len(payload["periods"]), 6)
        self.assertEqual(payload["periods"][0]["costMicros"], 1000000)
        self.assertEqual(payload["periods"][0]["conversions"], 10.5)

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_rejects_incomplete_or_invalid_snapshot(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.return_value = _hierarchy_response()
        incomplete = {
            **SCORECARD_ARGUMENTS,
            "periods": SCORECARD_ARGUMENTS["periods"][:-1],
        }
        with self.assertRaisesRegex(ToolError, "six required"):
            publish_account_scorecard(**incomplete)

        invalid = {
            **SCORECARD_ARGUMENTS,
            "periods": [
                {**period, "cost_micros": -1} if period["key"] == "mtd" else period
                for period in SCORECARD_ARGUMENTS["periods"]
            ],
        }
        with self.assertRaisesRegex(ToolError, "non-negative integer"):
            publish_account_scorecard(**invalid)
        mock_urlopen.assert_not_called()


class EnrollmentToolsTest(unittest.TestCase):
    portal_environment = {
        "RECOMMENDATION_CENTER_URL": "https://recommendations.example",
        "RECOMMENDATION_CENTER_INGESTION_KEY": "ingestion-secret",
        "RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN": "sites-secret",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
    }

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_syncs_enabled_client_descendants_without_enrolling(
        self, mock_get_service, mock_urlopen
    ):
        direct = _hierarchy_response()[0].results[0]
        nested = _hierarchy_response(
            customer_id="1697214266",
            level=3,
            descriptive_name="Nested Client",
        )[0].results[0]
        manager = _hierarchy_response(customer_id="2660187856", level=1, manager=True)[
            0
        ].results[0]
        mock_get_service.return_value.search_stream.return_value = [
            SimpleNamespace(results=[direct, nested, manager])
        ]
        mock_urlopen.return_value = _Response(
            {"synced": 2, "syncedAt": "2026-08-08T02:00:00Z"}
        )

        result = sync_customer_catalog()

        self.assertEqual(result["synced"], 2)
        self.assertFalse(result["enrollments_changed"])
        self.assertFalse(result["google_ads_changes_made"])
        search_call = mock_get_service.return_value.search_stream.call_args
        self.assertEqual(search_call.kwargs["customer_id"], "4599605095")
        outgoing = mock_urlopen.call_args.args[0]
        self.assertEqual(
            outgoing.full_url,
            "https://recommendations.example/api/accounts/catalog",
        )
        payload = json.loads(outgoing.data.decode("utf-8"))
        self.assertEqual(
            [account["customerId"] for account in payload["accounts"]],
            ["4357201747", "1697214266"],
        )

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    @patch("ads_mcp.tools.recommendations.utils.get_googleads_service")
    def test_sync_fails_closed_when_ads_lookup_errors(
        self, mock_get_service, mock_urlopen
    ):
        mock_get_service.return_value.search_stream.side_effect = RuntimeError(
            "unavailable"
        )

        with self.assertRaisesRegex(ToolError, "could not be completed"):
            sync_customer_catalog()
        mock_urlopen.assert_not_called()

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_gets_bounded_due_enrollment_queue(self, mock_urlopen):
        accounts = [{"customerId": "4357201747", "cadence": "daily"}]
        mock_urlopen.return_value = _Response(
            {
                "accounts": accounts,
                "checkedAt": "2026-08-08T02:00:00Z",
            },
            status=200,
        )

        result = get_due_enrollments(limit=4)

        self.assertEqual(result["accounts"], accounts)
        self.assertFalse(result["google_ads_changes_made"])
        outgoing = mock_urlopen.call_args.args[0]
        self.assertEqual(
            outgoing.full_url,
            "https://recommendations.example/api/accounts/due?limit=4",
        )
        self.assertEqual(outgoing.method, "GET")
        self.assertIsNone(outgoing.data)

    def test_rejects_invalid_due_queue_limit(self):
        for invalid in (0, 11, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ToolError, "integer"):
                    get_due_enrollments(limit=invalid)

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_records_idempotent_enrollment_run(self, mock_urlopen):
        mock_urlopen.return_value = _Response(
            {
                "recorded": True,
                "duplicate": False,
                "nextRunAt": "2026-08-09T10:00:00Z",
            }
        )

        result = record_enrollment_run(
            run_id="RUN-20260808-4357201747",
            customer_id="435-720-1747",
            status="succeeded",
            recommendation_count=1,
            note="One recommendation published for review.",
            completed_at="2026-08-08T10:00:00Z",
            data_through_date="2026-08-07",
            coverage=list(reversed(COVERAGE)),
        )

        self.assertTrue(result["recorded"])
        self.assertFalse(result["duplicate"])
        self.assertFalse(result["google_ads_changes_made"])
        outgoing = mock_urlopen.call_args.args[0]
        payload = json.loads(outgoing.data.decode("utf-8"))
        self.assertEqual(payload["runId"], "RUN-20260808-4357201747")
        self.assertEqual(payload["customerId"], "4357201747")
        self.assertEqual(payload["dataThroughDate"], "2026-08-07")
        self.assertEqual(len(payload["coverage"]), 10)
        self.assertEqual(
            payload["coverage"][0]["area"], "Conversion goals and tracking"
        )
        self.assertEqual(result["coverage_area_count"], 10)
        self.assertEqual(outgoing.method, "POST")

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_failed_run_may_record_without_coverage(self, mock_urlopen):
        mock_urlopen.return_value = _Response({"recorded": True, "duplicate": False})

        result = record_enrollment_run(
            run_id="RUN-20260808-4357201747-FAILED",
            customer_id="4357201747",
            status="failed",
            recommendation_count=0,
            note="Google Ads query failed closed.",
            completed_at="2026-08-08T10:00:00Z",
        )

        self.assertTrue(result["recorded"])
        self.assertEqual(result["coverage_area_count"], 0)
        payload = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "runId": "RUN-20260808-4357201747-FAILED",
                "customerId": "4357201747",
                "status": "failed",
                "recommendationCount": 0,
                "note": "Google Ads query failed closed.",
                "completedAt": "2026-08-08T10:00:00Z",
            },
        )

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_accepts_idempotent_duplicate_run_confirmation(self, mock_urlopen):
        mock_urlopen.return_value = _Response(
            {"recorded": True, "duplicate": True}, status=200
        )

        result = record_enrollment_run(
            run_id="RUN-20260808-4357201747",
            customer_id="4357201747",
            status="succeeded",
            recommendation_count=0,
            note="Existing recommendations refreshed; zero new review cards.",
            completed_at="2026-08-08T10:00:00Z",
            data_through_date="2026-08-07",
            coverage=COVERAGE,
        )

        self.assertTrue(result["recorded"])
        self.assertTrue(result["duplicate"])

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_rejects_non_boolean_duplicate_run_confirmation(self, mock_urlopen):
        mock_urlopen.return_value = _Response(
            {"recorded": True, "duplicate": "false"}, status=200
        )

        with self.assertRaisesRegex(ToolError, "invalid enrollment run"):
            record_enrollment_run(
                run_id="RUN-20260808-4357201747",
                customer_id="4357201747",
                status="succeeded",
                recommendation_count=0,
                note="Existing recommendations refreshed.",
                completed_at="2026-08-08T10:00:00Z",
                data_through_date="2026-08-07",
                coverage=COVERAGE,
            )

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_completed_run_requires_all_ten_coverage_areas(self, mock_urlopen):
        with self.assertRaisesRegex(ToolError, "all 10 analysis areas"):
            record_enrollment_run(
                run_id="RUN-20260808-4357201747",
                customer_id="4357201747",
                status="no_recommendation",
                recommendation_count=0,
                note="No recommendation met the evidence threshold.",
                completed_at="2026-08-08T10:00:00Z",
                data_through_date="2026-08-07",
                coverage=COVERAGE[:-1],
            )
        mock_urlopen.assert_not_called()

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_completed_run_requires_data_through_date(self, mock_urlopen):
        with self.assertRaisesRegex(ToolError, "data_through_date is required"):
            record_enrollment_run(
                run_id="RUN-20260808-4357201747",
                customer_id="4357201747",
                status="succeeded",
                recommendation_count=1,
                note="One recommendation published.",
                completed_at="2026-08-08T10:00:00Z",
                coverage=COVERAGE,
            )
        mock_urlopen.assert_not_called()

    @patch.dict("os.environ", portal_environment, clear=True)
    @patch("ads_mcp.tools.recommendations.request.urlopen")
    def test_rejects_malformed_analysis_coverage(self, mock_urlopen):
        cases = {
            "duplicate": [*COVERAGE[:-1], COVERAGE[0]],
            "unknown": [
                *COVERAGE[:-1],
                {
                    "area": "audiences",
                    "status": "reviewed",
                    "note": "Checked.",
                },
            ],
            "bad status": [
                {**COVERAGE[0], "status": "skipped"},
                *COVERAGE[1:],
            ],
            "blank note": [
                {**COVERAGE[0], "note": " "},
                *COVERAGE[1:],
            ],
            "extra property": [
                {**COVERAGE[0], "extra": "not allowed"},
                *COVERAGE[1:],
            ],
            "non-object": ["bad", *COVERAGE[1:]],
        }

        for label, coverage in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ToolError):
                    record_enrollment_run(
                        run_id="RUN-20260808-4357201747",
                        customer_id="4357201747",
                        status="no_recommendation",
                        recommendation_count=0,
                        note="No recommendation met the evidence threshold.",
                        completed_at="2026-08-08T10:00:00Z",
                        data_through_date="2026-08-07",
                        coverage=coverage,
                    )
        mock_urlopen.assert_not_called()

    def test_rejects_invalid_enrollment_run_arguments(self):
        with self.assertRaisesRegex(ToolError, "run_id"):
            record_enrollment_run(
                "bad id",
                "4357201747",
                "succeeded",
                1,
                "note",
                "2026-08-08T10:00:00Z",
            )
        with self.assertRaisesRegex(ToolError, "status"):
            record_enrollment_run(
                "RUN-20260808-4357201747",
                "4357201747",
                "pending",
                1,
                "note",
                "2026-08-08T10:00:00Z",
            )
        with self.assertRaisesRegex(ToolError, "recommendation_count"):
            record_enrollment_run(
                "RUN-20260808-4357201747",
                "4357201747",
                "succeeded",
                -1,
                "note",
                "2026-08-08T10:00:00Z",
            )
        with self.assertRaisesRegex(ToolError, "must have recommendation_count 0"):
            record_enrollment_run(
                "RUN-20260808-4357201747",
                "4357201747",
                "no_recommendation",
                1,
                "note",
                "2026-08-08T10:00:00Z",
                "2026-08-07",
                COVERAGE,
            )
        with self.assertRaisesRegex(ToolError, "UTC offset"):
            record_enrollment_run(
                "RUN-20260808-4357201747-FAILED",
                "4357201747",
                "failed",
                0,
                "note",
                "2026-08-08T10:00:00",
            )
