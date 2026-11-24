"""Negative keyword behavior with real protobufs and a fake Ads boundary."""

import importlib
import os
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

ENV = {
    "GOOGLE_ADS_MCP_AUTH_MODE": "google_oauth",
    "GOOGLE_ADS_MCP_ENABLE_NEGATIVE_KEYWORD_WRITES": "true",
    "GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS": "1234567890",
    "GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY": "test-only-signing-material-32-characters",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "4599605095",
}
ARGS = {
    "customer_id": "123-456-7890",
    "campaign_id": "987654321",
    "keywords": [{"text": "free cars", "match_type": "EXACT"}],
}


class FakeAds:
    """External calls only; production code builds real Google Ads messages."""

    def __init__(self):
        self.real = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="test-token",
            use_proto_plus=True,
            version="v24",
        )
        self.enums = self.real.enums
        self.existing = {}
        self.calls = []
        self.queries = []
        self.campaign_name = "Search - New Cars"
        self.campaign_status = "ENABLED"
        self.channel = "SEARCH"
        self.ad_group_campaign = 987654321
        self.fail_validation = False
        self.fail_apply = False
        self.commit_then_timeout = False
        self.hide_readback = False
        self.in_hierarchy = True

    def get_type(self, name):
        return self.real.get_type(name)

    def get_service(self, name, **kwargs):
        return self

    def search_stream(self, customer_id, query, **kwargs):
        if customer_id not in {"1234567890", "4599605095"}:
            raise AssertionError("Unexpected customer")
        self.queries.append(query)
        row = self.get_type("GoogleAdsRow")
        if "FROM customer_client " in query:
            row.customer_client.id = 1234567890
            row.customer_client.level = 1
            row.customer_client.manager = False
            row.customer_client.status = "ENABLED"
            rows = [row] if self.in_hierarchy else []
        elif "FROM customer " in query:
            row.customer.id = 1234567890
            row.customer.descriptive_name = "Example Dealer"
            row.customer.currency_code = "USD"
            row.customer.time_zone = "America/New_York"
            row.customer.status = "ENABLED"
            row.customer.manager = False
            rows = [row]
        elif "FROM campaign " in query:
            row.campaign.id = 987654321
            row.campaign.name = self.campaign_name
            row.campaign.status = self.campaign_status
            row.campaign.advertising_channel_type = self.channel
            rows = [row]
        elif "FROM ad_group " in query:
            row.ad_group.id = 444555666
            row.ad_group.name = "New Cars"
            row.ad_group.status = "ENABLED"
            row.campaign.id = self.ad_group_campaign
            rows = [row]
        else:
            level = (
                "ad_group_criterion"
                if "FROM ad_group_criterion " in query
                else "campaign_criterion"
            )
            rows = []
            if not (
                self.hide_readback
                and any(not c.validate_only for c in self.calls)
            ):
                for (text, match_type), resource in self.existing.items():
                    item = self.get_type("GoogleAdsRow")
                    criterion = getattr(item, level)
                    criterion.keyword.text = text
                    criterion.keyword.match_type = match_type
                    criterion.resource_name = resource
                    rows.append(item)
        return [SimpleNamespace(results=rows)]

    def _mutate(self, request, retry, timeout):
        if retry is not None:
            raise AssertionError("Mutations must disable automatic retries")
        if request.partial_failure:
            raise AssertionError("Mutations must be atomic")
        self.calls.append(request)
        if request.validate_only:
            if self.fail_validation:
                raise ValueError("Google rejected the keyword")
            return SimpleNamespace(results=[])
        if self.fail_apply:
            raise TimeoutError("Connection lost before confirmation")
        results = []
        for operation in request.operations:
            criterion = operation.create
            if not criterion.negative:
                raise AssertionError("Only negative creates are allowed")
            name = (
                f"customers/1234567890/campaignCriteria/987654321~{len(self.existing) + 1}"
                if hasattr(criterion, "campaign")
                else f"customers/1234567890/adGroupCriteria/444555666~{len(self.existing) + 1}"
            )
            self.existing[
                (criterion.keyword.text, criterion.keyword.match_type.name)
            ] = name
            results.append(SimpleNamespace(resource_name=name))
        if self.commit_then_timeout:
            raise TimeoutError("Server committed, response was lost")
        return SimpleNamespace(results=results)

    mutate_campaign_criteria = _mutate
    mutate_ad_group_criteria = _mutate


class NegativeKeywordsTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Import inside setup so the first RED run reports a missing feature.
        self.n = importlib.import_module("ads_mcp.tools.negatives")
        self.ads = FakeAds()
        for target, value in (
            ("utils.get_googleads_client", self.ads),
            ("get_access_token", SimpleNamespace(token="interactive-token")),
        ):
            mock = patch(
                f"ads_mcp.tools.negatives.{target}", return_value=value
            )
            mock.start()
            self.addCleanup(mock.stop)

    def preview(self, **updates):
        return self.n.preview_add_negative_keywords(**(ARGS | updates))

    def apply(self, preview):
        return self.n.apply_add_negative_keywords(
            preview_token=preview["preview_token"], approved=True
        )

    def test_stream_retains_service_until_all_batches_are_consumed(self):
        class EphemeralService:
            def search_stream(self, **kwargs):
                reference = weakref.ref(self)

                def batches():
                    if reference() is None:
                        raise RuntimeError(
                            "Stream removed (Channel deallocated!)"
                        )
                    yield SimpleNamespace(results=["row"])

                return batches()

        class UncachedClient:
            def get_service(self, name):
                return EphemeralService()

        self.assertEqual(
            self.n._rows(UncachedClient(), "1234567890", "query"), ["row"]
        )

    def test_campaign_preview_validates_without_creating(self):
        result = self.preview()
        self.assertEqual(result["target"]["account_name"], "Example Dealer")
        self.assertEqual(result["target"]["campaign_name"], "Search - New Cars")
        self.assertEqual(
            result["additions"], [{"text": "free cars", "match_type": "EXACT"}]
        )
        self.assertFalse(result["google_ads_changes_made"])
        self.assertEqual(self.ads.existing, {})
        self.assertEqual(len(self.ads.calls), 1)
        request = self.ads.calls[0]
        self.assertTrue(request.validate_only)
        self.assertEqual(
            request.operations[0].create.campaign,
            "customers/1234567890/campaigns/987654321",
        )
        self.assertTrue(request.operations[0].create.negative)

    def test_ad_group_apply_creates_only_negative_keyword_and_verifies(self):
        preview = self.preview(ad_group_id="444555666")
        result = self.apply(preview)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["verified"])
        self.assertTrue(result["google_ads_changes_made"])
        criterion = self.ads.calls[-1].operations[0].create
        self.assertEqual(
            criterion.ad_group, "customers/1234567890/adGroups/444555666"
        )
        self.assertEqual(criterion.keyword.match_type.name, "EXACT")
        self.assertIn(("free cars", "EXACT"), self.ads.existing)

    def test_phrase_and_broad_match_are_preserved(self):
        preview = self.preview(
            keywords=[
                {"text": "jobs", "match_type": "PHRASE"},
                {"text": "manual", "match_type": "BROAD"},
            ]
        )
        self.apply(preview)
        self.assertEqual(
            set(self.ads.existing), {("jobs", "PHRASE"), ("manual", "BROAD")}
        )

    def test_valid_large_unicode_batch_can_be_approved_and_applied(self):
        preview = self.preview(
            keywords=[
                {"text": "车" * 78 + str(i), "match_type": "EXACT"}
                for i in range(100)
            ]
        )
        self.assertTrue(self.apply(preview)["verified"])
        self.assertEqual(len(self.ads.existing), 100)

    def test_unsupported_campaign_match_semantics_are_rejected(self):
        for channel in ("DISPLAY", "VIDEO", "DEMAND_GEN"):
            self.ads.channel = channel
            with self.subTest(channel=channel), self.assertRaises(ToolError):
                self.preview()
        self.ads.channel = "SHOPPING"
        with self.assertRaises(ToolError):
            self.preview(ad_group_id="444555666")
        self.assertEqual(self.ads.calls, [])

    def test_duplicate_input_and_existing_negatives_are_skipped(self):
        self.ads.existing[("Free Cars", "EXACT")] = "existing"
        result = self.preview(
            keywords=[
                {"text": "free cars", "match_type": "EXACT"},
                {"text": "jobs", "match_type": "PHRASE"},
                {"text": "jobs", "match_type": "PHRASE"},
            ]
        )
        self.assertEqual(
            result["additions"], [{"text": "jobs", "match_type": "PHRASE"}]
        )
        self.assertEqual(len(result["already_present"]), 1)
        self.assertEqual(result["duplicates_removed"], 1)

    def test_replay_and_concurrent_addition_do_not_create_again(self):
        preview = self.preview()
        self.apply(preview)
        writes_before = sum(not call.validate_only for call in self.ads.calls)
        replay = self.apply(preview)
        self.assertEqual(replay["status"], "already_present")
        self.assertFalse(replay["google_ads_changes_made"])
        self.assertEqual(
            sum(not call.validate_only for call in self.ads.calls),
            writes_before,
        )

    def test_preexisting_keyword_removed_after_preview_is_not_added(self):
        self.ads.existing[("free cars", "EXACT")] = "existing"
        preview = self.preview()
        self.ads.existing.clear()
        result = self.apply(preview)
        self.assertEqual(self.ads.existing, {})
        self.assertFalse(result["google_ads_changes_made"])

    def test_unapproved_apply_never_calls_google(self):
        preview = self.preview()
        before = len(self.ads.queries)
        with self.assertRaises(ToolError):
            self.n.apply_add_negative_keywords(preview["preview_token"])
        self.assertEqual(len(self.ads.queries), before)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_expired_and_tampered_previews_never_write(self):
        with patch("ads_mcp.tools.negatives.time.time", return_value=1000):
            preview = self.preview()
        with patch("ads_mcp.tools.negatives.time.time", return_value=1601):
            with self.assertRaises(ToolError):
                self.apply(preview)
        preview = self.preview()
        preview["preview_token"] = "x" + preview["preview_token"][1:]
        with self.assertRaises(ToolError):
            self.apply(preview)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_other_authenticated_caller_cannot_apply_preview(self):
        preview = self.preview()
        with patch(
            "ads_mcp.tools.negatives.get_access_token",
            return_value=SimpleNamespace(token="different-user"),
        ):
            with self.assertRaises(ToolError):
                self.apply(preview)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_disabled_writes_and_worker_auth_are_rejected(self):
        for key, value in (
            ("GOOGLE_ADS_MCP_ENABLE_NEGATIVE_KEYWORD_WRITES", "false"),
            ("GOOGLE_ADS_MCP_AUTH_MODE", "service_jwt"),
            ("GOOGLE_ADS_MCP_AUTH_MODE", "none"),
            ("GOOGLE_ADS_SERVICE_REFRESH_TOKEN", "offline-token"),
            ("GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS", ""),
            ("GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS", "9999999999"),
            ("GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY", "short"),
        ):
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, {key: value}),
            ):
                with self.assertRaises(ToolError):
                    self.preview()
        self.assertEqual(self.ads.calls, [])
        self.assertEqual(self.ads.queries, [])

    def test_anonymous_caller_cannot_preview(self):
        with patch(
            "ads_mcp.tools.negatives.get_access_token", return_value=None
        ):
            with self.assertRaises(ToolError):
                self.preview()
        self.assertEqual(self.ads.queries, [])

    def test_allowlist_is_rechecked_at_apply(self):
        preview = self.preview()
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS": "9999999999"},
        ):
            with self.assertRaises(ToolError):
                self.apply(preview)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_existing_service_account_restriction_also_limits_writes(self):
        with patch.dict(
            os.environ, {"GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "9999999999"}
        ):
            with self.assertRaises(ToolError):
                self.preview()
        self.assertEqual(self.ads.queries, [])

    def test_all_mcc_clients_mode_still_checks_live_hierarchy(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS": "",
                "GOOGLE_ADS_MCP_NEGATIVES_ALLOW_MCC_DESCENDANTS": "true",
            },
        ):
            preview = self.preview()
            self.ads.in_hierarchy = False
            with self.assertRaises(ToolError):
                self.apply(preview)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_changed_target_and_ad_group_in_wrong_campaign_are_rejected(self):
        preview = self.preview()
        self.ads.campaign_name = "Renamed since preview"
        with self.assertRaises(ToolError):
            self.apply(preview)
        self.ads.ad_group_campaign = 111222333
        with self.assertRaises(ToolError):
            self.preview(ad_group_id="444555666")
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_removed_campaign_is_rejected(self):
        self.ads.campaign_status = "REMOVED"
        with self.assertRaises(ToolError):
            self.preview()
        self.assertEqual(self.ads.calls, [])

    def test_invalid_keywords_are_rejected_before_network(self):
        for keywords in (
            [],
            [{"text": "", "match_type": "EXACT"}],
            [{"text": "[jobs]", "match_type": "EXACT"}],
            [{"text": "jobs", "match_type": "UNKNOWN"}],
            [{"text": "jobs"}],
            [{"text": "x" * 81, "match_type": "EXACT"}],
            [{"text": "word " * 11, "match_type": "EXACT"}],
            [{"text": "jobs", "match_type": "EXACT"}] * 101,
        ):
            with self.subTest(keywords=keywords), self.assertRaises(ToolError):
                self.preview(keywords=keywords)
        self.assertEqual(self.ads.queries, [])

    def test_google_validation_failure_never_returns_applicable_preview(self):
        self.ads.fail_validation = True
        with self.assertRaises(ToolError):
            self.preview()
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_lost_response_after_commit_is_observed_without_claiming_creation(
        self,
    ):
        preview = self.preview()
        self.ads.commit_then_timeout = True
        result = self.apply(preview)
        self.assertEqual(result["status"], "observed_present")
        self.assertTrue(result["verified"])
        self.assertIsNone(result["google_ads_changes_made"])
        self.assertEqual(sum(not c.validate_only for c in self.ads.calls), 1)

    def test_timeout_and_failed_readback_remain_uncertain(self):
        for flag in ("fail_apply", "hide_readback"):
            with self.subTest(flag=flag):
                self.ads.existing.clear()
                self.ads.fail_apply = False
                self.ads.hide_readback = False
                preview = self.preview()
                setattr(self.ads, flag, True)
                result = self.apply(preview)
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
