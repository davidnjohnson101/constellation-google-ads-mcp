"""Remove credentials before log records reach any application handler."""

import logging
import re
import traceback

_SECRET = r"(?:access_token|refresh_token|id_token|client_secret|client_assertion|api_key|developer_token)"
_PLAIN = re.compile(rf"""(?i)(\b{_SECRET}["']?\s*[:=]\s*["']?)([^\s&"'<>}},)]+)""")
_ENCODED = re.compile(
    rf"(?i)(\b{_SECRET}(?:%22|%27)?(?:%3A|%3D)(?:%20|%22|%27)*)(.*?)(?=%26|%22|%27|\s|$)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[^\s\"'<>,}]+")


def redact(value: str) -> str:
    """Preserve diagnostics while replacing credential values, including URLs."""
    value = _ENCODED.sub(r"\1[REDACTED]", value)
    value = _PLAIN.sub(r"\1[REDACTED]", value)
    return _BEARER.sub(r"\1[REDACTED]", value)


def configure_log_redaction() -> None:
    """Compose with existing record factories and cover independent handlers."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_constellation_redaction", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    factory._constellation_redaction = True
    logging.setLogRecordFactory(factory)
    # Both client package names occur in deployed FastMCP dependencies.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
