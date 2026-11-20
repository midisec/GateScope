from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest


@pytest.mark.parametrize("failure", [None, "smoke", "issue", "revoke"])
@pytest.mark.parametrize("mode", ["default", "rate", "queue"])
def test_deployment_smoke_revokes_only_the_new_client_even_on_failure(monkeypatch, failure, mode):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    module = importlib.import_module("verify_deployed_mcp")
    inserted, revoked, invoked = [], [], []

    class Connection:
        def execute(self, sql, values):
            assert "INSERT INTO mcp_clients" in sql
            inserted.append(values[0])

    class Pool:
        @contextmanager
        def transaction(self, tenant, user):
            yield Connection()

    class Repository:
        def __init__(self, pool):
            pass

        def create_token(self, tenant, user, client, **kwargs):
            assert kwargs["ttl"].total_seconds() == 3600
            assert kwargs["scopes"] == ("mcp:connect", "market:read", "analysis:read")
            if failure == "issue":
                raise RuntimeError("DO_NOT_REPORT_PRIVATE_TEXT")
            return SimpleNamespace(secret="in-memory-only")

        def revoke_client(self, tenant, user, client):
            revoked.append(client)
            if failure == "revoke":
                raise RuntimeError("DO_NOT_REPORT_PRIVATE_TEXT")
            return SimpleNamespace(status="REVOKED")

        def list_tokens(self, tenant, user, client):
            return [SimpleNamespace(revoked_at=True)]

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            assert revoked
            return SimpleNamespace(status_code=401)

    def smoke(*args):
        invoked.append(args[2:4])
        if failure == "smoke":
            raise RuntimeError("DO_NOT_REPORT_PRIVATE_TEXT")
        return {"ok": True}

    monkeypatch.setattr(module, "resolve_owner", lambda pool, name: (UUID(int=2), UUID(int=3)))
    monkeypatch.setattr(module, "PostgresMcpRepository", Repository)
    monkeypatch.setattr(module, "smoke_run", smoke)
    monkeypatch.setattr(module.httpx, "Client", HttpClient)
    result = module.verify(Pool(), "https://43.108.44.74/mcp", "owner",
                           coinglass_recovery=mode == "rate", coinglass_queue=mode == "queue")
    assert revoked == inserted and len(inserted) == 1
    assert result["ok"] is (failure is None)
    assert result["temporary_client_revoked"] is (failure != "revoke")
    assert "DO_NOT_REPORT_PRIVATE_TEXT" not in repr(result)
    assert "in-memory-only" not in repr(result)
    assert result["existing_clients_modified"] is False and result["openai_called"] is False
    if failure != "issue":
        first, second = ("SOL_USDT", "SOL_USDT") if mode == "queue" else (
            "BTC_USDT", "ETH_USDT" if mode == "rate" else "龙虾_USDT",
        )
        assert invoked == [(first, "futures:usdt"), (second, "futures:usdt")]
