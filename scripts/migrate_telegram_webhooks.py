#!/usr/bin/env python3
"""Repoint existing ACTIVE bots during host migration, without sending messages."""
import argparse
import hashlib
import hmac
from urllib.parse import urlsplit

import psycopg
import requests

from prepare_service_envs import SECRET_DIR, read_env
from gatescope.application.notifications import TelegramBotAdmin
from gatescope.credential_crypto import CredentialEnvelope, RsaOaepCredentialDecryptor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-origin", required=True)
    parser.add_argument("--new-origin", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if urlsplit(args.new_origin).scheme != "https":
        raise ValueError("HTTPS is required")
    env = read_env(SECRET_DIR / "formal-runtime.env")
    if env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/") != args.new_origin.rstrip("/"):
        raise ValueError("New origin must match the deployed API configuration")
    decryptor = RsaOaepCredentialDecryptor.from_base64_der(env["GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY"], env["GATESCOPE_TELEGRAM_KEY_VERSION"])
    session = requests.Session()
    session.trust_env = False
    admin = TelegramBotAdmin(session=session)
    with psycopg.connect(env["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg:", "postgresql:")) as conn:
        rows = conn.execute("SELECT id,tenant_id,webhook_id,secret_ciphertext,secret_nonce,wrapped_data_key,encryption_context,key_version,secret_version,webhook_secret_hash FROM telegram_bot_configs WHERE status='ACTIVE'").fetchall()
        for row in rows:
            config_id, tenant_id, hook_id, ciphertext, nonce, wrapped, context, version, serial, secret_hash = row
            expected = f"gatescope:v1:tenant:{tenant_id}:telegram-config:{config_id}:secret:{serial}"
            if not hmac.compare_digest(context, expected):
                raise ValueError("Telegram envelope context mismatch")
            material = decryptor.reveal(CredentialEnvelope(bytes(ciphertext), bytes(nonce), bytes(wrapped), version), context=context.encode())
            if not hmac.compare_digest(hashlib.sha256(material.api_secret.encode()).hexdigest(), secret_hash):
                raise ValueError("Telegram webhook secret mismatch")
            api = f"https://api.telegram.org/bot{material.api_key}/getWebhookInfo"
            response = session.get(api, timeout=(5, 15))
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise RuntimeError("TG_WEBHOOK_INSPECTION_FAILED")
            current = response.json()["result"]["url"]
            destination = args.new_origin.rstrip("/") + f"/api/v2/telegram/webhook/{hook_id}"
            old = args.old_origin.rstrip("/") + f"/api/v2/telegram/webhook/{hook_id}"
            if current not in (old, destination):
                raise RuntimeError("Existing webhook differs from the scoped migration origins; review required")
            if args.apply and current != destination:
                admin.set_webhook(material.api_key, destination, material.api_secret)
                check = session.get(api, timeout=(5, 15))
                if check.status_code != 200 or check.json().get("ok") is not True or check.json()["result"]["url"] != destination:
                    raise RuntimeError("TG_WEBHOOK_VERIFICATION_FAILED")
                conn.execute("UPDATE telegram_bot_configs SET webhook_configured_at=now(),updated_at=now(),etag=etag+1 WHERE id=%s", (config_id,))
            print("Existing Telegram bot: secret decryption valid; webhook " + ("verified on new origin" if args.apply else "eligible for migration"))
    print(f"Active Telegram bots checked: {len(rows)}; no test messages or trade commands sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Telegram migration failed: {type(exc).__name__}; upstream URLs and secrets suppressed")
        raise SystemExit(1) from None
