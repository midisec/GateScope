"""Only compile the pure request predicate; never import/run browser acceptance."""
import ast
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@pytest.fixture
def fence():
    source = Path(__file__).resolve().parents[2] / "scripts" / "smoke_paper_browser.py"
    tree = ast.parse(source.read_text())
    predicate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "allowed_request")
    module = ast.Module(body=[predicate], type_ignores=[])
    namespace = {"urlsplit": urlsplit}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["allowed_request"]


@pytest.mark.parametrize("path,method", [
    ("/api/v2/paper/orders", "POST"), ("/api/v2/paper/orders/a:cancel", "POST"),
    ("/api/v2/paper/positions/a:close", "POST"), ("/api/v2/paper-accounts", "POST"),
    ("/api/v2/mcp/clients", "POST"), ("/api/v2/watchlists", "POST"),
    ("/api/v2/paper/orders/a", "DELETE"), ("/api/v2/chart-layouts/a", "PATCH"),
    ("/auth/mfa-methods:enroll", "POST"), ("/auth/recovery-codes:regenerate", "POST"),
])
def test_acceptance_cannot_mutate_any_order_account_or_settings(fence, path, method):
    assert not fence("https://example.test", "https://example.test" + path, method, {"anything": "fixture"})


def test_acceptance_allows_only_encrypted_auth_and_existing_mfa(fence):
    origin = "https://example.test"
    assert fence(origin, origin + "/auth/login", "POST", {"encrypted_submission": {"ciphertext": "fixture"}})
    assert not fence(origin, origin + "/auth/login", "POST", {"username": "fixture", "password": "fixture"})
    assert not fence(origin, origin + "/auth/login", "POST", {"encrypted_submission": "bad"})
    assert fence(origin, origin + "/auth/mfa/verify", "POST", {"code": "000000"})
    assert not fence(origin, origin + "/auth/mfa/verify", "POST", {"code": "000000", "other": "field"})
    assert fence(origin, origin + "/auth/logout", "POST")


def test_acceptance_allows_same_origin_reads_and_read_only_preview_only(fence):
    origin = "https://example.test"
    assert fence(origin, origin + "/api/v2/paper/accounts/id/snapshot", "GET")
    assert fence(origin, origin + "/api/v2/indicators/preview", "POST", {"instrument_id": "fixture"})
    assert not fence(origin, origin + "/api/v2/indicators/preview", "POST", None)
    for url in ["https://external.test/", "http://example.test/", "https://user@example.test/", "https://example.test.evil.test/"]:
        assert not fence(origin, url, "GET")
