from pathlib import Path
import re


def test_oauth_log_format_has_no_sensitive_request_material():
    config = (Path(__file__).resolve().parents[2] / "deploy/nginx/gatescope.conf").read_text()
    log_format = config.split("log_format gatescope_oauth_discovery", 1)[1].split(";", 1)[0]
    variables = set(re.findall(r"\$[a-z0-9_]+", log_format))
    assert variables == {"$time_iso8601", "$request_method", "$uri", "$status", "$upstream_status",
                         "$request_time", "$gatescope_oauth_source", "$gatescope_oauth_agent"}
    assert not any(word in log_format for word in ("$request_uri", "$args", "$query_string", "$http_", "$cookie_", "$remote_addr"))


def test_exact_mcp_alias_retains_authenticated_upstream():
    config = (Path(__file__).resolve().parents[2] / "deploy/nginx/gatescope.conf").read_text()
    for path in ("/mcp", "/mcp/"):
        block = config.split(f"location = {path} {{", 1)[1].split("}", 1)[0]
        assert "proxy_pass http://127.0.0.1:17800/mcp;" in block
        assert "proxy_set_header Authorization $http_authorization;" in block
        assert "client_max_body_size 256k;" in block
        assert "gatescope_oauth_discovery;" in block
        assert "error_log /dev/null crit;" in block
