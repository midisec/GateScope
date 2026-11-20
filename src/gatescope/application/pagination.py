"""Tamper-evident cursors bound to the authenticated query shape."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class CursorCodec:
    secret: bytes
    ttl_seconds: int = 3_600

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("cursor signing secret must contain at least 32 bytes")
        if self.ttl_seconds < 60:
            raise ValueError("cursor ttl must be at least 60 seconds")

    def encode(self, position: Mapping[str, Any], binding: Mapping[str, Any], *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else now
        payload = {
            "binding": binding,
            "expires_at": issued_at + self.ttl_seconds,
            "position": position,
            "version": 1,
        }
        encoded = _b64(_json(payload))
        signature = _b64(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def decode(self, token: str, binding: Mapping[str, Any], *, now: int | None = None) -> dict[str, Any]:
        if len(token) > 1_024 or token.count(".") != 1:
            raise InvalidCursor("cursor is malformed")
        encoded, supplied_signature = token.split(".", 1)
        expected = _b64(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected):
            raise InvalidCursor("cursor signature is invalid")
        try:
            payload = json.loads(_unb64(encoded))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidCursor("cursor payload is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise InvalidCursor("cursor version is invalid")
        if payload.get("binding") != dict(binding):
            raise InvalidCursor("cursor does not belong to this query")
        current_time = int(time.time()) if now is None else now
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, int) or expires_at < current_time:
            raise InvalidCursor("cursor has expired")
        position = payload.get("position")
        if not isinstance(position, dict):
            raise InvalidCursor("cursor position is invalid")
        return position


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")
