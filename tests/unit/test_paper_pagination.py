from datetime import datetime, timezone
from uuid import uuid4

import pytest

from gatescope.domain.paper_pagination import decode_cursor, encode_cursor, scope_digest


def test_cursor_roundtrip_is_filter_bound_and_time_aware():
    at = datetime.now(timezone.utc)
    identity = uuid4()
    scope = scope_digest(owner=uuid4(), account=uuid4(), status="FILLED")
    cursor = encode_cursor(scope, at, identity, at)
    assert decode_cursor(cursor, scope) == (at, identity, at)
    with pytest.raises(ValueError):
        decode_cursor(cursor, scope_digest(owner=uuid4()))


@pytest.mark.parametrize("value", [None, "", 123, "x" * 513, "%%%", "e30", "W10"])
def test_bad_cursor_is_not_a_sql_parameter_or_unbounded_decode(value):
    with pytest.raises(ValueError, match="CURSOR_INVALID"):
        decode_cursor(value, "scope")


def test_naive_time_cannot_cross_timezone_boundaries():
    at = datetime.now()
    with pytest.raises(ValueError):
        decode_cursor(encode_cursor("s", at, uuid4(), at), "s")
