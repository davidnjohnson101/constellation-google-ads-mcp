"""Approved management against real protobufs and fake external boundaries."""

import importlib
import asyncio
import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from tests.tools.negatives_test import ENV, FakeAds

C = "1234567890"
CAM = "987654321"
G = "444555666"
BASE = {"customer_id": C, "campaign_id": CAM}
GROUP = BASE | {"ad_group_id": G}


class ManagementAds(FakeAds):
    def __init__(self):
        super().__init__()
        self.criteria = {}
        self.amount = 100000000
        self.period = "DAILY"
        self.members = [(int(CAM), "Search - New Cars", "ENABLED")]
        self.currency = "USD"
        self.kind = "KEYWORD"

    def search_stream(self, customer_id, query, **kwargs):
        row = self.get_type("GoogleAdsRow")
        if "FROM campaign_budget " in query:
            b = row.campaign_budget
            b.resource_name = f"customers/{C}/campaignBudgets/77"
            b.id = 77
            b.name = "Daily budget"
            b.amount_micros = self.amount
            b.period = self.period
            b.status = "ENABLED"
            b.explicitly_shared = len(self.members) > 1
            return [SimpleNamespace(results=[row])]
        if "FROM campaign " in query and "campaign.campaign_budget =" in query:
            rows = []
            for cid, name, status in self.members:
                item = self.get_type("GoogleAdsRow")
                item.campaign.id = cid
                item.campaign.name = name
                item.campaign.status = status
                rows.append(item)
            return [SimpleNamespace(results=rows)]
        if (
            "FROM ad_group_criterion " in query
            or "FROM campaign_criterion " in query
        ):
            level = (
                "ad_group_criterion"
                if "FROM ad_group_criterion " in query
                else "campaign_criterion"
            )
            rows = []
            for cid, data in self.criteria.items():
                if data["level"] != level or data["status"] == "REMOVED":
                    continue
                item = self.get_type("GoogleAdsRow")
                cr = getattr(item, level)
                cr.resource_name = self.resource(cid, level)
                cr.criterion_id = int(cid)
                cr.type_ = self.kind
                cr.negative = data["negative"]
                cr.status = data["status"]
                cr.keyword.text = data["text"]
                cr.keyword.match_type = data["match_type"]
                rows.append(item)
            return [SimpleNamespace(results=rows)]
        result = super().search_stream(customer_id, query, **kwargs)
        for batch in result:
            for item in batch.results:
                if "FROM campaign " in query:
                    item.campaign.resource_name = (
                        f"customers/{C}/campaigns/{CAM}"
                    )
                    item.campaign.campaign_budget = (
                        f"customers/{C}/campaignBudgets/77"
                    )
                if "FROM customer " in query:
                    item.customer.currency_code = self.currency
        return result

    def resource(self, cid, level="ad_group_criterion"):
        path, owner = (
            ("adGroupCriteria", G)
            if level == "ad_group_criterion"
            else ("campaignCriteria", CAM)
        )
        return f"customers/{C}/{path}/{owner}~{cid}"

    def keyword(
        self,
        cid="11",
        negative=False,
        level="ad_group_criterion",
        status="ENABLED",
    ):
        self.criteria[cid] = dict(
            text="bmw dealer",
            match_type="EXACT",
            negative=negative,
            level=level,
            status=status,
        )

    def _mutate(self, request, retry, timeout):
        assert retry is None and request.partial_failure is False
        self.calls.append(request)
        if request.validate_only:
            if self.fail_validation:
                raise ValueError("rejected")
            return SimpleNamespace(results=[])
        if self.fail_apply:
            raise TimeoutError()
        results = []
        for op in request.operations:
            name = type(op).__name__
            if name == "CampaignOperation":
                assert list(op.update_mask.paths) == ["status"]
                self.campaign_status = op.update.status.name
                self.members[0] = (
                    int(CAM),
                    self.campaign_name,
                    self.campaign_status,
                )
                resource = op.update.resource_name
            elif name == "CampaignBudgetOperation":
                assert list(op.update_mask.paths) == ["amount_micros"]
                self.amount = op.update.amount_micros
                resource = op.update.resource_name
            elif op.remove:
                cid = op.remove.rsplit("~", 1)[1]
                self.criteria[cid]["status"] = "REMOVED"
                resource = op.remove
            elif op.update.resource_name:
                assert list(op.update_mask.paths) == ["status"]
                cid = op.update.resource_name.rsplit("~", 1)[1]
                self.criteria[cid]["status"] = op.update.status.name
                resource = op.update.resource_name
            else:
                cr = op.create
                assert not cr.negative
                cid = str(100 + len(self.criteria))
                self.criteria[cid] = dict(
                    text=cr.keyword.text,
                    match_type=cr.keyword.match_type.name,
                    negative=False,
                    status=cr.status.name,
                    level="ad_group_criterion",
                )
                resource = self.resource(cid)
            results.append(SimpleNamespace(resource_name=resource))
        if self.commit_then_timeout:
            raise TimeoutError()
        return SimpleNamespace(results=results)

    mutate_campaigns = _mutate
    mutate_campaign_budgets = _mutate
    mutate_ad_group_criteria = _mutate
    mutate_campaign_criteria = _mutate


class ManagementTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            ENV
            | {
                "GOOGLE_ADS_MCP_ENABLE_MANAGEMENT_WRITES": "true",
                "GOOGLE_ADS_MCP_STORAGE_REDIS_URL": "redis://test",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        self.m = importlib.import_module("ads_mcp.tools.management")
        self.ads = ManagementAds()
        self.claimed = set()

        def claim(preview_id, expires_at):
            if preview_id in self.claimed:
                raise ToolError("Already consumed")
            self.claimed.add(preview_id)

        for name, value in (
            (
                "ads_mcp.tools.negatives.get_access_token",
                SimpleNamespace(token="actor"),
            ),
        ):
            p = patch(name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch(
            "ads_mcp.tools.management.utils.get_googleads_client",
            side_effect=lambda: self.ads,
        )
        p.start()
        self.addCleanup(p.stop)
        self.real_claim = self.m._claim
        self.real_permit = self.m._permit
        self.real_guard = self.m._guard
        p = patch("ads_mcp.tools.management._permit")
        p.start()
        self.addCleanup(p.stop)

        @contextmanager
        def guard(customer):
            yield SimpleNamespace(owned=lambda: True)

        p = patch("ads_mcp.tools.management._guard", side_effect=guard)
        p.start()
        self.addCleanup(p.stop)
        p = patch("ads_mcp.tools.management._claim", side_effect=claim)
        p.start()
        self.addCleanup(p.stop)

    def apply(self, p, **kwargs):
        return self.m.apply_approved_change(
            p["preview_token"], approved=True, **kwargs
        )

    def add(self, **kwargs):
        return self.m.preview_add_keywords(
            **(
                GROUP
                | {
                    "keywords": [{"text": "bmw dealer", "match_type": "EXACT"}],
                    "status": "PAUSED",
                }
                | kwargs
            )
        )

    def test_add_preview_and_apply_preserve_explicit_status_and_match(self):
        p = self.add()
        self.assertTrue(p["approval_required"])
        self.assertFalse(p["google_ads_changes_made"])
        self.assertTrue(all(c.validate_only for c in self.ads.calls))
        self.assertEqual(p["change"]["status"], "PAUSED")
        self.assertEqual(self.apply(p)["status"], "applied")
        self.assertEqual(
            next(iter(self.ads.criteria.values()))["status"], "PAUSED"
        )

    def test_positive_keyword_pause_and_enable(self):
        self.ads.keyword()
        for status in ("PAUSED", "ENABLED"):
            p = self.m.preview_set_keyword_status(
                **GROUP, criterion_id="11", status=status
            )
            self.assertEqual(self.apply(p)["status"], "applied")
            self.assertEqual(self.ads.criteria["11"]["status"], status)

    def test_remove_requires_correct_negative_flag_and_exact_keyword(self):
        self.ads.keyword(negative=True, level="campaign_criterion")
        with self.assertRaises(ToolError):
            self.m.preview_remove_keyword(
                **BASE, criterion_id="11", negative=False
            )
        p = self.m.preview_remove_keyword(
            **BASE, criterion_id="11", negative=True
        )
        self.assertTrue(p["change"]["negative"])
        self.assertEqual(self.apply(p)["status"], "applied")
        self.assertEqual(self.ads.criteria["11"]["status"], "REMOVED")

    def test_nonkeyword_or_negative_status_update_rejected(self):
        self.ads.keyword(negative=True)
        with self.assertRaises(ToolError):
            self.m.preview_set_keyword_status(
                **GROUP, criterion_id="11", status="PAUSED"
            )
        self.ads.keyword()
        self.ads.kind = "PLACEMENT"
        with self.assertRaises(ToolError):
            self.m.preview_remove_keyword(
                **GROUP, criterion_id="11", negative=False
            )

    def test_campaign_preview_carries_budget_and_only_updates_status(self):
        p = self.m.preview_set_campaign_status(**BASE, status="PAUSED")
        self.assertEqual(p["before"]["budget"]["amount_micros"], 100000000)
        self.assertEqual(self.apply(p)["status"], "applied")
        self.assertEqual(self.ads.campaign_status, "PAUSED")

    def test_shared_budget_preview_lists_every_affected_campaign(self):
        self.ads.members.append((123, "Other campaign", "PAUSED"))
        p = self.m.preview_set_daily_budget(
            **BASE, amount="125.123456", currency_code="USD"
        )
        self.assertEqual(len(p["before"]["budget"]["affected_campaigns"]), 2)
        self.assertEqual(p["change"]["amount_micros"], 125123456)
        self.assertEqual(self.apply(p)["status"], "applied")
        self.assertEqual(self.ads.amount, 125123456)

    def test_invalid_budget_amount_currency_and_total_period_fail_before_write(
        self,
    ):
        for amount in (
            "0",
            "-1",
            "1.0000001",
            "1e2",
            "NaN",
            "9223372036855",
            1.25,
        ):
            with self.subTest(amount=amount), self.assertRaises(ToolError):
                self.m.preview_set_daily_budget(
                    **BASE, amount=amount, currency_code="USD"
                )
        with self.assertRaises(ToolError):
            self.m.preview_set_daily_budget(
                **BASE, amount="100", currency_code="EUR"
            )
        self.ads.period = "CUSTOM_PERIOD"
        with self.assertRaises(ToolError):
            self.m.preview_set_daily_budget(
                **BASE, amount="100", currency_code="USD"
            )
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_stale_budget_membership_amount_or_campaign_status_blocks_apply(
        self,
    ):
        for change in (
            lambda: self.ads.members.append((1, "New member", "ENABLED")),
            lambda: setattr(self.ads, "amount", 200000000),
            lambda: setattr(self.ads, "campaign_status", "PAUSED"),
        ):
            self.ads = ManagementAds()
            p = self.m.preview_set_daily_budget(
                **BASE, amount="120", currency_code="USD"
            )
            change()
            with self.assertRaises(ToolError):
                self.apply(p)
            self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_approval_signature_caller_expiry_and_feature_gate(self):
        p = self.add()
        with self.assertRaises(ToolError):
            self.m.apply_approved_change(p["preview_token"])
        with self.assertRaises(ToolError):
            self.m.apply_approved_change(
                p["preview_token"] + "a", approved=True
            )
        with (
            patch(
                "ads_mcp.tools.negatives.get_access_token",
                return_value=SimpleNamespace(token="other"),
            ),
            self.assertRaises(ToolError),
        ):
            self.apply(p)
        with (
            patch(
                "ads_mcp.tools.management.time.time",
                return_value=p["expires_at"],
            ),
            self.assertRaises(ToolError),
        ):
            self.apply(p)
        with (
            patch.dict(
                os.environ, {"GOOGLE_ADS_MCP_ENABLE_MANAGEMENT_WRITES": "false"}
            ),
            self.assertRaises(ToolError),
        ):
            self.apply(p)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_lost_response_reports_observed_and_consumes_approval(self):
        p = self.add()
        self.ads.commit_then_timeout = True
        result = self.apply(p)
        self.assertEqual(result["status"], "observed_applied")
        self.assertIsNone(result["google_ads_changes_made"])
        # Even restoring the original state cannot make this approval reusable.
        self.ads.criteria.clear()
        with self.assertRaises(ToolError):
            self.apply(p)
        self.assertEqual(sum(not c.validate_only for c in self.ads.calls), 1)

    def test_validation_rejection_and_unconfirmed_write_never_retry(self):
        p = self.add()
        self.ads.fail_validation = True
        with self.assertRaises(ToolError):
            self.apply(p)
        self.ads.fail_validation = False
        self.ads.fail_apply = True
        result = self.apply(p)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["verified"])
        self.assertEqual(sum(not c.validate_only for c in self.ads.calls), 1)

    def test_duplicate_add_is_noop_and_new_direct_negative_uses_old_tools(self):
        self.ads.keyword()
        p = self.add(status="ENABLED")
        self.assertEqual(p["change"]["additions"], [])
        self.assertEqual(self.apply(p)["status"], "no_change")
        self.assertEqual(self.ads.calls, [])

    def test_noop_preview_can_only_be_consumed_once(self):
        self.ads.keyword()
        p = self.add(status="ENABLED")
        self.assertEqual(self.apply(p)["status"], "no_change")
        with self.assertRaises(ToolError):
            self.apply(p)

    def test_wire_approval_requires_actual_boolean(self):
        p = self.add()
        tool = FunctionTool.from_function(self.m.apply_approved_change)
        for value in (1, "yes", "true"):
            with self.subTest(value=value), self.assertRaises(Exception):
                asyncio.run(
                    tool.run(
                        {"preview_token": p["preview_token"], "approved": value}
                    )
                )
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_negative_wire_selector_and_existing_apply_are_strict(self):
        self.ads.keyword(negative=True, level="campaign_criterion")
        tool = FunctionTool.from_function(self.m.preview_remove_keyword)
        with self.assertRaises(Exception):
            asyncio.run(
                tool.run(BASE | {"criterion_id": "11", "negative": "yes"})
            )
        from ads_mcp.tools import negatives

        preview = negatives.preview_add_negative_keywords(
            **BASE, keywords=[{"text": "irrelevant", "match_type": "EXACT"}]
        )
        tool = FunctionTool.from_function(negatives.apply_add_negative_keywords)
        with self.assertRaises(Exception):
            asyncio.run(
                tool.run(
                    {"preview_token": preview["preview_token"], "approved": 1}
                )
            )
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_redis_guard_contention_and_release(self):
        import redis

        with patch.object(redis.Redis, "from_url") as factory:
            store = factory.return_value
            lock = store.lock.return_value
            lock.acquire.return_value = False
            with self.assertRaises(ToolError):
                with self.real_guard(C):
                    self.fail("Contended guard entered")
            lock.acquire.return_value = True
            with self.real_guard(C) as held:
                self.assertIs(held, lock)
            lock.release.assert_called_once()
            self.assertGreater(store.lock.call_args.kwargs["timeout"], 630)

    def test_overlapping_shared_budget_apply_is_blocked_before_second_write(
        self,
    ):
        p1 = self.m.preview_set_daily_budget(
            **BASE, amount="120", currency_code="USD"
        )
        p2 = self.m.preview_set_daily_budget(
            **BASE, amount="150", currency_code="USD"
        )
        held = set()

        @contextmanager
        def guard(customer):
            if customer in held:
                raise ToolError("Another apply holds this account")
            held.add(customer)
            try:
                yield SimpleNamespace(owned=lambda: True)
            finally:
                held.remove(customer)

        original = self.ads.mutate_campaign_budgets
        overlap = []

        def mutate(request, retry, timeout):
            if request.validate_only and not overlap:
                overlap.append(True)
                with self.assertRaises(ToolError):
                    self.apply(p2)
            return original(request, retry, timeout)

        with (
            patch("ads_mcp.tools.management._guard", side_effect=guard),
            patch.object(
                self.ads, "mutate_campaign_budgets", side_effect=mutate
            ),
        ):
            self.assertEqual(self.apply(p1)["status"], "applied")
        self.assertEqual(sum(not c.validate_only for c in self.ads.calls), 1)

    def test_large_unicode_preview_can_be_decoded(self):
        p = self.add(
            keywords=[
                {"text": "车" * 78 + str(i), "match_type": "EXACT"}
                for i in range(100)
            ]
        )
        self.assertEqual(self.apply(p)["status"], "applied")

    def test_expiry_during_revalidation_prevents_live_write(self):
        p = self.add()
        original = self.ads._mutate

        def validate(request, retry, timeout):
            result = original(request, retry, timeout)
            self.clock.return_value = p["expires_at"]
            return result

        with (
            patch(
                "ads_mcp.tools.management.time.time",
                return_value=p["expires_at"] - 1,
            ) as self.clock,
            patch.object(
                self.ads, "mutate_ad_group_criteria", side_effect=validate
            ),
        ):
            with self.assertRaises(ToolError):
                self.apply(p)
        self.assertFalse(any(not c.validate_only for c in self.ads.calls))

    def test_real_claim_uses_atomic_redis_and_fails_closed(self):
        import redis

        # Exercise actual consumption, while the rest of the suite stubs Redis.
        with patch.object(redis.Redis, "from_url") as factory:
            store = factory.return_value.__enter__.return_value
            store.getdel.return_value = b"1"
            self.real_claim("one-use", 2000000000)
            store.set.return_value = True
            self.real_permit("new-preview", 2000000000)
            self.assertTrue(store.set.call_args.kwargs["nx"])
            store.getdel.return_value = None
            with self.assertRaises(ToolError):
                self.real_claim("one-use", 2000000000)
            factory.side_effect = TimeoutError()
            with self.assertRaises(ToolError):
                self.real_claim("one-use", 2000000000)
