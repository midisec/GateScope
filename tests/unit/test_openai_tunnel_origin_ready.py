"""Origin readiness checks use fake HTTPS and time, never a live service."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("origin_ready_gate", Path(__file__).resolve().parents[2] / "scripts/check_openai_tunnel_origin_ready.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
ORIGIN = "https://43.108.44.74"


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def response(path):
    if path == gate.PRM_PATH:
        payload = {"resource": ORIGIN + "/mcp", "authorization_servers": [ORIGIN]}
    elif path == gate.AS_PATH:
        payload = {"issuer": ORIGIN, "authorization_endpoint": ORIGIN + "/oauth/authorize",
                   "token_endpoint": ORIGIN + "/oauth/token", "registration_endpoint": ORIGIN + "/oauth/register",
                   "code_challenge_methods_supported": ["S256"]}
    else:
        assert path == "/mcp"
        return 401, {"www-authenticate": f'Bearer resource_metadata="{ORIGIN}{gate.PRM_PATH}"'}, b""
    return 200, {"content-type": "application/json"}, json.dumps(payload).encode()


def test_retries_502_then_validates_three_canonical_endpoints():
    clock, calls = Clock(), []

    def fetch(origin, path, timeout):
        assert origin == ORIGIN and 0 < timeout <= 3
        calls.append(path)
        return (502, {}, b"DO_NOT_LOG_RESPONSE") if len(calls) == 1 else response(path)

    result = gate.wait_until_ready(ORIGIN, 45, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert result == {"ready": True, "attempts": 2}
    assert calls == [gate.PRM_PATH, gate.PRM_PATH, gate.AS_PATH, "/mcp"]
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("path,field,bad", [
    (gate.PRM_PATH, "resource", "https://unexpected.invalid/mcp"),
    (gate.PRM_PATH, "authorization_servers", ["https://unexpected.invalid"]),
    (gate.AS_PATH, "issuer", ORIGIN + "/mcp"),
    (gate.AS_PATH, "token_endpoint", "https://unexpected.invalid/oauth/token"),
    (gate.AS_PATH, "registration_endpoint", None),
    (gate.AS_PATH, "code_challenge_methods_supported", ["plain"]),
    (gate.AS_PATH, "code_challenge_methods_supported", "S256"),
])
def test_bad_metadata_fails_closed_without_sleep_or_following_discovered_urls(path, field, bad):
    clock, calls = Clock(), []

    def fetch(origin, requested, timeout):
        calls.append(requested)
        status, headers, body = response(requested)
        if requested == path:
            payload = json.loads(body)
            payload[field] = bad
            body = json.dumps(payload).encode()
        return status, headers, body

    result = gate.wait_until_ready(ORIGIN, 45, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert not result["ready"] and result["attempts"] == 1
    assert not clock.sleeps and len(calls) <= 2
    assert "unexpected.invalid" not in json.dumps(result)


@pytest.mark.parametrize("location", [ORIGIN + gate.PRM_PATH + "/", "https://unexpected.invalid/private"])
def test_refuses_same_origin_and_cross_origin_redirects(location):
    clock, calls = Clock(), []

    def fetch(origin, path, timeout):
        calls.append(path)
        return 302, {"location": location}, b"private body"

    result = gate.wait_until_ready(ORIGIN, 45, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert result == {"ready": False, "attempts": 1, "failure": "redirect_refused"}
    assert calls == [gate.PRM_PATH] and not clock.sleeps


def test_retry_after_larger_than_budget_does_not_issue_an_early_retry():
    clock, calls = Clock(), []

    def fetch(origin, path, timeout):
        calls.append(timeout)
        return 429, {"retry-after": "60"}, b""

    result = gate.wait_until_ready(ORIGIN, 5, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert result == {"ready": False, "attempts": 1, "failure": "deadline_exceeded"}
    assert calls == [3.0] and clock.sleeps == [5.0] and clock.now == 5.0


def test_network_failures_and_total_deadline_are_bounded():
    clock, timeouts = Clock(), []

    def fetch(origin, path, timeout):
        timeouts.append(timeout)
        clock.now += min(timeout, 0.4)
        raise gate.ProbeError("network_unavailable", retryable=True)

    result = gate.wait_until_ready(ORIGIN, 4, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert not result["ready"] and result["failure"] == "deadline_exceeded"
    assert result["attempts"] < gate.MAX_ATTEMPTS and clock.now == 4
    assert all(0 < timeout <= 3 for timeout in timeouts)


def test_metadata_call_that_consumes_deadline_does_not_start_next_request():
    clock, calls = Clock(), []

    def fetch(origin, path, timeout):
        calls.append(path)
        clock.now = 5
        return response(path)

    result = gate.wait_until_ready(ORIGIN, 5, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert result["failure"] == "deadline_exceeded" and calls == [gate.PRM_PATH]


@pytest.mark.parametrize("headers", [{}, {"www-authenticate": 'Bearer realm="only-pat"'},
    {"www-authenticate": 'Bearer resource_metadata="https://unexpected.invalid/metadata"'}])
def test_requires_401_with_exact_same_origin_discovery_challenge(headers):
    clock = Clock()

    def fetch(origin, path, timeout):
        return (401, headers, b"secret") if path == "/mcp" else response(path)

    result = gate.wait_until_ready(ORIGIN, 5, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert result["failure"] == "mcp_discovery_challenge_missing" and not clock.sleeps


@pytest.mark.parametrize("body", [b"[]", b"not-json", b"x" * (gate.BODY_LIMIT + 1)])
def test_metadata_response_is_bounded_and_never_printed(body):
    clock = Clock()
    result = gate.wait_until_ready(ORIGIN, 5, fetch=lambda *_: (200, {"content-type": "application/json"}, body),
                                   clock=clock, sleep=clock.sleep)
    assert not result["ready"] and result["attempts"] == 1 and not clock.sleeps
    assert "not-json" not in json.dumps(result)


@pytest.mark.parametrize("origin", ["http://43.108.44.74", ORIGIN + "/", ORIGIN + "/mcp", ORIGIN + "?key=hidden",
    "https://user:password@43.108.44.74", ORIGIN + "#fragment", "https://43.108.44.74:70000"])
def test_invalid_origin_never_reaches_network(origin):
    def forbidden(*_):
        pytest.fail("invalid origin reached network")
    assert gate.wait_until_ready(origin, 5, fetch=forbidden)["failure"] == "invalid_origin"


def test_https_transport_has_no_proxy_or_secret_headers_and_does_not_read_redirect_body(monkeypatch):
    recorded = {}

    class Response:
        status = 302
        def getheaders(self):
            return [("Location", "https://unexpected.invalid")]
        def read(self, *_):
            pytest.fail("redirect body should not be read")

    class Connection:
        def __init__(self, hostname, port, **kwargs):
            recorded.update(hostname=hostname, port=port, timeout=kwargs["timeout"])
        def request(self, method, path, **kwargs):
            recorded.update(method=method, path=path, headers=kwargs["headers"])
        def getresponse(self):
            return Response()
        def close(self):
            recorded["closed"] = True

    monkeypatch.setattr(gate.http.client, "HTTPSConnection", Connection)
    monkeypatch.setenv("HTTPS_PROXY", "http://never-use.invalid")
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "never-send-test-fixture")
    assert gate.https_get(ORIGIN, gate.PRM_PATH, 2)[0] == 302
    assert recorded["hostname"] == "43.108.44.74" and recorded["port"] == 443
    assert recorded["method"] == "GET" and recorded["path"] == gate.PRM_PATH and recorded["closed"]
    assert set(recorded["headers"]) == {"Accept", "User-Agent", "Connection"}
    assert "never-send-test-fixture" not in json.dumps(recorded)


def test_cli_hard_deadline_is_safe_and_restores_alarm_state(monkeypatch, capsys):
    calls, handlers = [], []
    old_handler = object()

    def install_handler(signum, handler):
        assert signum == gate.signal.SIGALRM
        handlers.append(handler)
        return old_handler

    def expire_during_request(*_):
        handlers[0](gate.signal.SIGALRM, None)

    monkeypatch.setattr("sys.argv", ["check-origin-ready.py", "--origin", ORIGIN, "--timeout", "45"])
    monkeypatch.setattr(gate.signal, "signal", install_handler)
    monkeypatch.setattr(gate.signal, "setitimer", lambda *args: calls.append(args))
    monkeypatch.setattr(gate, "wait_until_ready", expire_during_request)
    assert gate.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ready": False, "attempts": 0, "failure": "deadline_exceeded"}
    assert not captured.err
    assert calls == [(gate.signal.ITIMER_REAL, 45.0), (gate.signal.ITIMER_REAL, 0)]
    assert handlers[-1] is old_handler


def test_cli_unexpected_errors_never_expose_exception_content(monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("private-test-response-never-print")

    monkeypatch.setattr("sys.argv", ["check-origin-ready.py", "--origin", ORIGIN])
    monkeypatch.setattr(gate.signal, "signal", lambda *_: gate.signal.SIG_DFL)
    monkeypatch.setattr(gate.signal, "setitimer", lambda *_: None)
    monkeypatch.setattr(gate, "wait_until_ready", fail)
    assert gate.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ready": False, "attempts": 0, "failure": "probe_failed"}
    assert not captured.err
