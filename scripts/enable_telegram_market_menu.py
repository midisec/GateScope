"""Enable callbacks/menu for existing active bots, without changing bindings or sending messages."""
import argparse,hashlib,hmac
import psycopg,requests
from prepare_service_envs import SECRET_DIR,read_env
from gatescope.application.notifications import TelegramBotAdmin
from gatescope.credential_crypto import CredentialEnvelope,RsaOaepCredentialDecryptor


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    env=read_env(SECRET_DIR/'formal-runtime.env')
    decryptor=RsaOaepCredentialDecryptor.from_base64_der(env['GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY'],env['GATESCOPE_TELEGRAM_KEY_VERSION'])
    session=requests.Session();session.trust_env=False;admin=TelegramBotAdmin(session=session)
    count=0
    with psycopg.connect(env['GATESCOPE_MIGRATION_DATABASE_URL']) as conn:
        rows=conn.execute("SELECT id,tenant_id,webhook_id,secret_ciphertext,secret_nonce,wrapped_data_key,encryption_context,key_version,secret_version,webhook_secret_hash FROM telegram_bot_configs WHERE status='ACTIVE'").fetchall()
        for config,tenant,hook,cipher,nonce,wrapped,context,version,serial,digest in rows:
            expected=f'gatescope:v1:tenant:{tenant}:telegram-config:{config}:secret:{serial}'
            if not hmac.compare_digest(context,expected):raise RuntimeError('TG_CONTEXT_MISMATCH')
            material=decryptor.reveal(CredentialEnvelope(bytes(cipher),bytes(nonce),bytes(wrapped),version),context=context.encode())
            if not hmac.compare_digest(hashlib.sha256(material.api_secret.encode()).hexdigest(),digest):raise RuntimeError('TG_SECRET_MISMATCH')
            destination=env['GATESCOPE_PUBLIC_ORIGIN'].rstrip('/')+f'/api/v2/telegram/webhook/{hook}'
            endpoint=f'https://api.telegram.org/bot{material.api_key}/getWebhookInfo'
            response=session.get(endpoint,timeout=(3,10));payload=response.json()
            if response.status_code!=200 or payload.get('ok') is not True or payload['result']['url']!=destination:raise RuntimeError('TG_WEBHOOK_NOT_EXPECTED_ORIGIN')
            if args.apply:
                admin.set_webhook(material.api_key,destination,material.api_secret)
                admin.set_market_commands(material.api_key)
                check=session.get(endpoint,timeout=(3,10));info=check.json()
                if check.status_code!=200 or info.get('ok') is not True or 'callback_query' not in info['result'].get('allowed_updates',[]):raise RuntimeError('TG_CALLBACK_NOT_ENABLED')
            count+=1
    print(f'Active bots verified: {count}; callbacks/menu '+('enabled' if args.apply else 'eligible')+'; no bindings changed or messages sent')


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(f'Telegram setup failed: {type(exc).__name__}; secrets and upstream details suppressed')
        raise SystemExit(1) from None
