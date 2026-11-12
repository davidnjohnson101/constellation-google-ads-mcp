"""Regressions for container-local OAuth state and cross-service collisions."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from ads_mcp.auth_storage import create_client_storage


class PersistentAuthStorageTest(unittest.TestCase):
    def test_cloud_run_without_shared_storage_fails_closed(self):
        with patch.dict(os.environ, {"K_SERVICE": "google-ads-mcp"}, clear=True):
            with self.assertRaisesRegex(ValueError, "persistent|shared"):
                create_client_storage()

    def test_cloud_run_cannot_use_memory_storage(self):
        env = {
            "K_SERVICE": "google-ads-mcp",
            "GOOGLE_ADS_MCP_STORAGE_TYPE": "memory",
            "GOOGLE_ADS_MCP_JWT_SIGNING_KEY": "test-key-" * 8,
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "persistent|shared"):
                create_client_storage()

    def test_oauth_record_survives_new_store_and_is_namespaced(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "GOOGLE_ADS_MCP_STORAGE_TYPE": "filetree",
                "GOOGLE_ADS_MCP_STORAGE_PATH": directory,
                "GOOGLE_ADS_MCP_JWT_SIGNING_KEY": "test-key-" * 8,
                "GOOGLE_ADS_MCP_STORAGE_NAMESPACE": "constellation-ads",
            }

            async def verify():
                value = {"refresh_token": "synthetic-private-refresh"}
                first = create_client_storage()
                await first.put("same-client", value, collection="oauth", ttl=300)
                restarted = create_client_storage()
                self.assertEqual(
                    await restarted.get("same-client", collection="oauth"), value
                )
                os.environ["GOOGLE_ADS_MCP_STORAGE_NAMESPACE"] = "another-service"
                other = create_client_storage()
                self.assertIsNone(await other.get("same-client", collection="oauth"))
                self.assertTrue(
                    await restarted.delete("same-client", collection="oauth")
                )
                self.assertIsNone(await first.get("same-client", collection="oauth"))

            with patch.dict(os.environ, env, clear=True):
                asyncio.run(verify())
