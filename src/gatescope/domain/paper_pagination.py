"""Owner/filter-bound keyset cursors. Cursors confer no read authorization."""

import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID


def scope_digest(**values):
    return hashlib.sha256(
        json.dumps(values, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def encode_cursor(scope, moment, identity, snapshot):
    body = json.dumps(
        [1, scope, moment.isoformat(), str(identity), snapshot.isoformat()], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def decode_cursor(value, scope):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 512:
            raise ValueError()
        data = json.loads(base64.b64decode(value + "=" * ((-len(value)) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, list) or len(data) != 5 or data[:2] != [1, scope]:
            raise ValueError()
        at, identity, snapshot = (
            datetime.fromisoformat(data[2]),
            UUID(data[3]),
            datetime.fromisoformat(data[4]),
        )
        if at.tzinfo is None or snapshot.tzinfo is None or at > snapshot:
            raise ValueError()
        return at.astimezone(timezone.utc), identity, snapshot.astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeDecodeError) as exc:
        raise ValueError("PAPER_CURSOR_INVALID_OR_FILTER_CHANGED") from exc
