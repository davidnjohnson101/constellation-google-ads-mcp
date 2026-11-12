"""Explicit deployment check using synthetic data in an isolated Redis namespace.

Run in two separate processes with the same environment: write, then read-delete.
Never run automatically as part of the unit suite.
"""

import asyncio
import json
import sys

from ads_mcp.auth_storage import create_client_storage


async def check(mode):
    store = create_client_storage()
    key, collection = "repair-probe", "repair-check"
    value = {"refresh_token": "synthetic-persistence-check"}
    if mode == "write":
        await store.put(key, value, collection=collection, ttl=300)
        print("PASS: encrypted test record written with a five-minute expiry")
    else:
        assert await store.get(key, collection=collection) == value
        raw = await store.key_value.get(key, collection=collection)
        assert "synthetic-persistence-check" not in json.dumps(raw)
        assert raw.get("__encrypted_data__")
        _, remaining = await store.ttl(key, collection=collection)
        assert remaining is not None and 0 < remaining <= 300
        assert await store.delete(key, collection=collection)
        assert await store.get(key, collection=collection) is None
        print(
            "PASS: separate process recovered encrypted state; TTL and deletion verified"
        )


if __name__ == "__main__":
    asyncio.run(check(sys.argv[1]))
