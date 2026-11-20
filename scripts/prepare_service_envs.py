#!/usr/bin/env python3
"""Split the bootstrap secret bundle into least-privilege workload env files."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / "data" / "runtime-secrets"


def main() -> int:
    source = read_env(SECRET_DIR / "formal-runtime.env")
    ensure_telegram_wrap_key(source)
    ensure_submission_wrap_key(source)
    ensure_coinglass_wrap_key(source)
    public_origin = os.environ.get(
        "GATESCOPE_SERVICE_PUBLIC_ORIGIN",
        source.get("GATESCOPE_PUBLIC_ORIGIN", "http://43.155.154.25:17758"),
    )
    api_host = os.environ.get(
        "GATESCOPE_SERVICE_API_HOST", source.get("GATESCOPE_API_HOST", "0.0.0.0"),
    )
    if api_host not in {"127.0.0.1", "0.0.0.0"}:
        raise RuntimeError("GATESCOPE_SERVICE_API_HOST must be 127.0.0.1 or 0.0.0.0")
    telegram_ready = all(
        source.get(name, "")
        for name in (
            "GATESCOPE_TELEGRAM_BOT_USERNAME",
            "GATESCOPE_TELEGRAM_BOT_TOKEN",
            "GATESCOPE_TELEGRAM_WEBHOOK_ID",
            "GATESCOPE_TELEGRAM_WEBHOOK_SECRET",
        )
    )
    source["GATESCOPE_PUBLIC_ORIGIN"] = public_origin
    source["GATESCOPE_API_HOST"] = api_host
    if not telegram_ready:
        for name in (
            "GATESCOPE_TELEGRAM_BOT_USERNAME",
            "GATESCOPE_TELEGRAM_BOT_TOKEN",
            "GATESCOPE_TELEGRAM_WEBHOOK_ID",
            "GATESCOPE_TELEGRAM_WEBHOOK_SECRET",
            "GATESCOPE_TELEGRAM_ALLOWED_USER_IDS",
        ):
            source[name] = ""
    write_env(SECRET_DIR / "formal-runtime.env", source)
    telegram_api = {
        "GATESCOPE_TELEGRAM_BOT_USERNAME": source.get("GATESCOPE_TELEGRAM_BOT_USERNAME", "")
        if telegram_ready else "",
        "GATESCOPE_TELEGRAM_WEBHOOK_ID": source.get("GATESCOPE_TELEGRAM_WEBHOOK_ID", "")
        if telegram_ready else "",
        "GATESCOPE_TELEGRAM_WEBHOOK_SECRET": source.get("GATESCOPE_TELEGRAM_WEBHOOK_SECRET", "")
        if telegram_ready else "",
        "GATESCOPE_TELEGRAM_ALLOWED_USER_IDS": source.get("GATESCOPE_TELEGRAM_ALLOWED_USER_IDS", "")
        if telegram_ready else "",
    }
    environments = {
        "formal-api.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_DATABASE_URL"),
            "GATESCOPE_IDENTITY_KEK": required(source, "GATESCOPE_IDENTITY_KEK"),
            "GATESCOPE_IDENTITY_KEY_VERSION": required(source, "GATESCOPE_IDENTITY_KEY_VERSION"),
            "GATESCOPE_RECOVERY_PEPPER": required(source, "GATESCOPE_RECOVERY_PEPPER"),
            "GATESCOPE_CURSOR_SIGNING_KEY": required(source, "GATESCOPE_CURSOR_SIGNING_KEY"),
            "GATESCOPE_CREDENTIAL_WRAP_PUBLIC_KEY": required(
                source, "GATESCOPE_CREDENTIAL_WRAP_PUBLIC_KEY"
            ),
            "GATESCOPE_CREDENTIAL_KEY_VERSION": required(source, "GATESCOPE_CREDENTIAL_KEY_VERSION"),
            "GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY": required(
                source, "GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY"
            ),
            "GATESCOPE_TELEGRAM_KEY_VERSION": required(source, "GATESCOPE_TELEGRAM_KEY_VERSION"),
            "GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY": required(
                source, "GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY"
            ),
            "GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY": required(
                source, "GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY"
            ),
            "GATESCOPE_SUBMISSION_KEY_VERSION": required(source, "GATESCOPE_SUBMISSION_KEY_VERSION"),
            "GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY": required(
                source, "GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY"
            ),
            "GATESCOPE_COINGLASS_KEY_VERSION": required(source, "GATESCOPE_COINGLASS_KEY_VERSION"),
            "GATESCOPE_PUBLIC_ORIGIN": public_origin,
            "GATESCOPE_LOGIN_WORKSPACES": source.get("GATESCOPE_LOGIN_WORKSPACES", "default"),
            "HOST": api_host,
            "PORT": "17758",
            **telegram_api,
        },
        "formal-market.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_MARKET_DATABASE_URL"),
            "GATESCOPE_WORKER_ID": "formal-market-primary",
            "GATESCOPE_MARKET_UPDATE_SECONDS": "5",
            "GATESCOPE_CATALOG_REFRESH_SECONDS": "1800",
            "GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY": required(
                source, "GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY"
            ),
            "GATESCOPE_COINGLASS_KEY_VERSION": required(source, "GATESCOPE_COINGLASS_KEY_VERSION"),
        },
        "formal-strategy.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_STRATEGY_DATABASE_URL"),
            "GATESCOPE_WORKER_ID": "formal-strategy-primary",
            "LIVE_TRADING_ENABLED": "false",
            "GATESCOPE_EXECUTION_ENVIRONMENT": "PAPER",
        },
        "formal-paper.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_PAPER_DATABASE_URL"),
            "GATESCOPE_WORKER_ID": "formal-paper-primary",
            "LIVE_TRADING_ENABLED": "false",
            "GATESCOPE_EXECUTION_ENVIRONMENT": "PAPER",
        },
        "formal-notification.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_NOTIFICATION_DATABASE_URL"),
            "GATESCOPE_TELEGRAM_BOT_TOKEN": source.get("GATESCOPE_TELEGRAM_BOT_TOKEN", "")
            if telegram_ready else "",
            "GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY": required(
                source, "GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY"
            ),
            "GATESCOPE_TELEGRAM_KEY_VERSION": required(source, "GATESCOPE_TELEGRAM_KEY_VERSION"),
            "GATESCOPE_WORKER_ID": "formal-notification-primary",
            "LIVE_TRADING_ENABLED": "false",
        },
        "formal-executor-validation.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_EXECUTOR_DATABASE_URL"),
            "GATESCOPE_CREDENTIAL_WRAP_PRIVATE_KEY": required(
                source, "GATESCOPE_CREDENTIAL_WRAP_PRIVATE_KEY"
            ),
            "GATESCOPE_CREDENTIAL_KEY_VERSION": required(source, "GATESCOPE_CREDENTIAL_KEY_VERSION"),
            "GATESCOPE_WORKER_ID": "formal-credential-validator",
            "GATESCOPE_EXECUTOR_WORKLOAD": "credential-validation",
            "GATESCOPE_EXECUTION_ENVIRONMENT": "PAPER",
            "LIVE_TRADING_ENABLED": "false",
        },
        "formal-executor-testnet.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_EXECUTOR_DATABASE_URL"),
            "GATESCOPE_CREDENTIAL_WRAP_PRIVATE_KEY": required(
                source, "GATESCOPE_CREDENTIAL_WRAP_PRIVATE_KEY"
            ),
            "GATESCOPE_CREDENTIAL_KEY_VERSION": required(source, "GATESCOPE_CREDENTIAL_KEY_VERSION"),
            "GATESCOPE_WORKER_ID": "formal-testnet-executor",
            "GATESCOPE_EXECUTOR_WORKLOAD": "gate-testnet-execution",
            "GATESCOPE_EXECUTION_ENVIRONMENT": "TESTNET",
            "LIVE_TRADING_ENABLED": "false",
        },
        "formal-mcp.env": {
            "GATESCOPE_DATABASE_URL": required(source, "GATESCOPE_MCP_DATABASE_URL"),
            "GATESCOPE_MCP_PUBLIC_URL": source.get("GATESCOPE_MCP_PUBLIC_URL") or (
                public_origin.rstrip("/")+"/mcp" if public_origin.startswith("https://") else "http://127.0.0.1:17800/mcp"),
            "GATESCOPE_MCP_HOST": "127.0.0.1",
            "GATESCOPE_MCP_PORT": "17800",
            "LIVE_TRADING_ENABLED": "false",
        },
    }
    SECRET_DIR.chmod(0o700)
    for name, values in environments.items():
        write_env(SECRET_DIR / name, values)
    print(f"Prepared {len(environments)} workload environment files with mode 0600")
    print(
        "Telegram Web configuration is ready; no legacy Bot is configured"
        if not telegram_ready else "Telegram Web and legacy configuration are ready"
    )
    print("Browser encrypted-submission key is configured")
    return 0


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name:
            raise ValueError("runtime env contains an invalid line")
        values[name] = value
    return values


def required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def ensure_telegram_wrap_key(values: dict[str, str]) -> None:
    names = (
        "GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY",
        "GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY",
        "GATESCOPE_TELEGRAM_KEY_VERSION",
    )
    present = tuple(bool(values.get(name, "")) for name in names)
    if all(present):
        return
    if any(present):
        raise RuntimeError("Telegram wrapping key configuration is incomplete")
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
    private_der = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    values["GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY"] = base64.urlsafe_b64encode(public_der).decode().rstrip("=")
    values["GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY"] = base64.urlsafe_b64encode(private_der).decode().rstrip("=")
    values["GATESCOPE_TELEGRAM_KEY_VERSION"] = "telegram-local-v1"


def ensure_submission_wrap_key(values: dict[str, str]) -> None:
    names = (
        "GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY",
        "GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY",
        "GATESCOPE_SUBMISSION_KEY_VERSION",
    )
    present = tuple(bool(values.get(name, "")) for name in names)
    if all(present):
        return
    if any(present):
        raise RuntimeError("browser submission wrapping key configuration is incomplete")
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
    private_der = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    values["GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY"] = (
        base64.urlsafe_b64encode(public_der).decode().rstrip("=")
    )
    values["GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY"] = (
        base64.urlsafe_b64encode(private_der).decode().rstrip("=")
    )
    values["GATESCOPE_SUBMISSION_KEY_VERSION"] = "browser-submission-local-v1"


def ensure_coinglass_wrap_key(values: dict[str, str]) -> None:
    names = (
        "GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY",
        "GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY",
        "GATESCOPE_COINGLASS_KEY_VERSION",
    )
    present = tuple(bool(values.get(name, "")) for name in names)
    if all(present):
        return
    if any(present):
        raise RuntimeError("Coinglass wrapping key configuration is incomplete")
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
    private_der = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    values["GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY"] = (
        base64.urlsafe_b64encode(public_der).decode().rstrip("=")
    )
    values["GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY"] = (
        base64.urlsafe_b64encode(private_der).decode().rstrip("=")
    )
    values["GATESCOPE_COINGLASS_KEY_VERSION"] = "coinglass-local-v1"


def write_env(path: Path, values: dict[str, str]) -> None:
    for name, value in values.items():
        if not name or any(character in name for character in "=\n\r"):
            raise ValueError("invalid environment variable name")
        if any(character in value for character in "\n\r\x00"):
            raise ValueError(f"{name} contains an unsupported character")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
