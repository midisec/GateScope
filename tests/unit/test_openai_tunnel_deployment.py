"""Static deployment boundaries; no daemon, credentials, or network calls."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_tunnel_profile_keeps_credentials_separate_and_oauth_origin_consistent():
    profile = yaml.safe_load((ROOT / "deploy/tunnel-client/gatescope.example.yaml").read_text())
    assert profile["control_plane"]["base_url"] == "https://api.openai.com"
    assert profile["control_plane"]["api_key"] == "env:CONTROL_PLANE_API_KEY"
    assert profile["mcp"] == {
        "server_urls": [{"channel": "main", "url": "https://43.108.44.74/mcp"}],
    }
    assert profile["health"]["listen_addr"] == "127.0.0.1:17801"
    assert profile["admin_ui"]["open_browser"] is False
    # Default polling includes the auxiliary channel used by OAuth discovery.
    # Restricting polling to main breaks older gateway OAuth shims.
    assert "poll_channels" not in profile["control_plane"]


def test_tunnel_unit_does_not_share_owner_identity_or_other_workload_secrets():
    unit = (ROOT / "deploy/systemd/gatescope-tunnel.service").read_text()
    assert "DynamicUser=yes" in unit
    assert "ConditionFileNotEmpty=/etc/gatescope-tunnel/control-plane.env" in unit
    assert "EnvironmentFile=/etc/gatescope-tunnel/control-plane.env" in unit
    assert "formal-runtime.env" not in unit
    assert "formal-mcp.env" not in unit
    assert "--mcp.extra-headers" not in unit
    assert "--mcp.discovery-extra-headers" not in unit
    assert "--embedded-mcp-stub" not in unit
    assert "--cloudflared" not in unit
    assert "--harpoon.target" not in unit
    assert "--log.http-raw-unsafe=false" in unit
    assert "--allow-remote-ui=false" in unit
    assert "--harpoon.capture-payloads=false" in unit
    assert "--harpoon.hosts-include-loopback=false" in unit
    assert "--harpoon.hosts-include-private=false" in unit
    assert "--harpoon.hosts-include-regex" not in unit
    assert "--harpoon.hosts-include-suffix" not in unit
    assert "--harpoon.allow-plaintext-http" not in unit
    assert "--control-plane.poll-channel" not in unit
    assert "MemoryMax=256M" in unit
    assert "CPUQuota=50%" in unit
    assert "ProtectHome=yes" in unit
    assert "ProtectSystem=strict" in unit


def test_tunnel_waits_for_public_oauth_stack_at_startup():
    unit = (ROOT / "deploy/systemd/gatescope-tunnel.service").read_text()
    lines = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    for service in ("nginx.service", "gatescope-api.service", "gatescope-mcp.service"):
        assert service in lines["After"].split()
        assert service in lines["Wants"].split()
    assert "--mcp.startup-wait-timeout 30s" in lines["ExecStart"]
    assert lines["ExecStartPre"] == (
        "/usr/bin/python3 /usr/local/lib/gatescope-tunnel/check-origin-ready.py "
        "--origin https://43.108.44.74 --timeout 45"
    )
    assert lines["TimeoutStartSec"] == "60"
    assert (ROOT / "scripts/check_openai_tunnel_origin_ready.py").is_file()
