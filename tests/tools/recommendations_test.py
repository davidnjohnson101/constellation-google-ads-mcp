"""Tests for Recommendation Center publishing."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools.recommendations import publish_recommendation

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
}


class _Response:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(
            {
                "recommendation": {
                    "id": "REC-20260807-BMW-001",
                    "status": "pending_review",
                }
            }
        ).encode("utf-8")


def _hierarchy_response(
    customer_id="4357201747", level=1, manager=False, status="ENABLED"
):
    client = SimpleNamespace(
        id=int(customer_id),
        level=level,
        manager=manager,
        status=SimpleNamespace(name=status),
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
        mock_get_service.return_value.search_stream.return_value = (
            _hierarchy_response()
        )
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
        search_call = mock_get_service.return_value.search_stream.call_args
        self.assertEqual(search_call.kwargs["customer_id"], "4599605095")
        self.assertIn(
            "customer_client.id = 4357201747", search_call.kwargs["query"]
        )

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
        mock_get_service.return_value.search_stream.return_value = (
            _hierarchy_response(level=3)
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
        mock_get_service.return_value.search_stream.return_value = (
            _hierarchy_response(status="CANCELED")
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
        mock_get_service.return_value.search_stream.return_value = (
            _hierarchy_response(manager=True)
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
