"""Capture actual log output using synthetic credentials only."""

import importlib
import io
import logging
import unittest


class LogRedactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            module = importlib.import_module("ads_mcp.log_redaction")
        except ModuleNotFoundError:
            return  # Existing logging is exercised before the repair exists.
        module.configure_log_redaction()

    def setUp(self):
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger("repair-test.transport")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def test_http_request_query_token_is_removed(self):
        self.logger.info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"',
            "https://oauth2.googleapis.com/tokeninfo?access_token=synthetic-access&other=ok",
        )
        result = self.output.getvalue()
        self.assertNotIn("synthetic-access", result)
        self.assertIn("other=ok", result)
        self.assertIn("200 OK", result)

    def test_bearer_and_refresh_tokens_are_removed(self):
        self.logger.warning(
            'headers={"Authorization": "Bearer synthetic-bearer"} '
            'body={"refresh_token": "synthetic-refresh", "client_secret": "synthetic-secret"}'
        )
        result = self.output.getvalue()
        for credential in ("synthetic-bearer", "synthetic-refresh", "synthetic-secret"):
            self.assertNotIn(credential, result)

    def test_percent_encoded_query_token_is_removed(self):
        self.logger.error(
            "url=https://example.test/?access_token%3Dsynthetic-encoded%26state%3Dok"
        )
        self.assertNotIn("synthetic-encoded", self.output.getvalue())

    def test_exception_retains_diagnostic_but_not_credential(self):
        try:
            raise ValueError("invalid_grant: refresh_token=synthetic-exception-token")
        except ValueError:
            self.logger.exception("Upstream refresh failed")
        result = self.output.getvalue()
        self.assertNotIn("synthetic-exception-token", result)
        self.assertIn("invalid_grant", result)
        self.assertIn("Upstream refresh failed", result)

    def test_existing_record_factory_is_preserved(self):
        try:
            module = importlib.import_module("ads_mcp.log_redaction")
        except ModuleNotFoundError:
            self.skipTest("Factory composition is checked once redaction exists")
        original = logging.getLogRecordFactory()

        def custom_factory(*args, **kwargs):
            record = original(*args, **kwargs)
            record.deployment_label = "kept"
            return record

        try:
            logging.setLogRecordFactory(custom_factory)
            module.configure_log_redaction()
            record = logging.getLogRecordFactory()(
                "x", 20, "", 1, "access_token=synthetic", (), None
            )
            self.assertEqual(record.deployment_label, "kept")
            self.assertNotIn("synthetic", record.getMessage())
        finally:
            logging.setLogRecordFactory(original)
