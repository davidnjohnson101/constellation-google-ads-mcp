"""Tool discovery must distinguish interactive and read-only worker surfaces."""

import json
import os
import subprocess
import sys
import unittest

from ads_mcp.config import ToolsConfig


class NegativeToolMountingTest(unittest.TestCase):
    def mounted(self, path):
        # Different deployments run in separate processes. The legacy mounting
        # code filters module-level subservers destructively, so isolate them.
        env = os.environ.copy()
        env["GOOGLE_ADS_MCP_TOOLS_CONFIG"] = path
        env.pop("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID", None)
        env.pop("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET", None)
        env["GOOGLE_ADS_MCP_AUTH_MODE"] = "none"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio,json; from ads_mcp.coordinator import mcp; tools=asyncio.run(mcp.list_tools()); print(json.dumps({t.name: {'parameters': t.parameters, 'annotations': t.annotations.model_dump(by_alias=True)} for t in tools}))",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(result.stdout)

    def test_interactive_exposes_explicit_preview_and_write_annotations(
        self,
    ):
        tools = self.mounted("ads_mcp/tools_config.yaml")
        preview = tools["negatives_preview_add_negative_keywords"]
        apply = tools["negatives_apply_add_negative_keywords"]
        self.assertTrue(preview["annotations"]["readOnlyHint"])
        self.assertFalse(apply["annotations"]["readOnlyHint"])
        self.assertFalse(apply["annotations"]["idempotentHint"])
        self.assertEqual(
            apply["parameters"]["properties"]["approved"]["default"], False
        )
        self.assertEqual(
            set(apply["parameters"]["properties"]),
            {"preview_token", "approved"},
        )
        keyword = preview["parameters"]["properties"]["keywords"]["items"]
        self.assertEqual(set(keyword["required"]), {"text", "match_type"})
        self.assertFalse(keyword["additionalProperties"])

    def test_worker_and_fallback_never_expose_negative_keyword_writes(
        self,
    ):
        tools = self.mounted("ads_mcp/worker_tools_config.yaml")
        self.assertFalse(any(name.startswith("negatives_") for name in tools))
        self.assertFalse(ToolsConfig().is_namespace_enabled("negatives"))

    def test_management_catalog_has_only_previews_and_approved_apply(self):
        tools = self.mounted("ads_mcp/tools_config.yaml")
        names = {name for name in tools if name.startswith("management_")}
        self.assertEqual(
            names,
            {
                "management_preview_add_keywords",
                "management_preview_set_keyword_status",
                "management_preview_remove_keyword",
                "management_preview_set_campaign_status",
                "management_preview_set_daily_budget",
                "management_apply_approved_change",
            },
        )
        apply = tools["management_apply_approved_change"]
        self.assertFalse(apply["annotations"]["readOnlyHint"])
        self.assertTrue(apply["annotations"]["destructiveHint"])
        self.assertFalse(
            apply["parameters"]["properties"]["approved"]["default"]
        )
        self.assertEqual(
            set(apply["parameters"]["properties"]),
            {"preview_token", "approved"},
        )
        for name in names - {"management_apply_approved_change"}:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        self.assertFalse(
            any(
                name.startswith("management_")
                for name in self.mounted("ads_mcp/worker_tools_config.yaml")
            )
        )
        self.assertFalse(ToolsConfig().is_namespace_enabled("management"))
