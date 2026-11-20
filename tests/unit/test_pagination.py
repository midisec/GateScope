from __future__ import annotations

import pytest

from gatescope.application.pagination import CursorCodec, InvalidCursor


def test_cursor_is_query_bound_tamper_evident_and_expires() -> None:
    codec = CursorCodec(b"a" * 32, ttl_seconds=60)
    token = codec.encode({"after": 42}, {"instrument": "btc"}, now=1_000)
    assert codec.decode(token, {"instrument": "btc"}, now=1_059) == {"after": 42}
    with pytest.raises(InvalidCursor, match="query"):
        codec.decode(token, {"instrument": "eth"}, now=1_059)
    changed = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidCursor, match="signature"):
        codec.decode(changed, {"instrument": "btc"}, now=1_059)
    with pytest.raises(InvalidCursor, match="expired"):
        codec.decode(token, {"instrument": "btc"}, now=1_061)
