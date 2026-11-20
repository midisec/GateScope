"""Admin acceptance on the disposable gatescope_test database, never production."""
from __future__ import annotations

import base64
import os
import time
from uuid import uuid4

import psycopg
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from urllib.parse import urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from apps.api.formal_app import create_formal_app
from gatescope.security_secrets import totp_code
from gatescope.security_secrets import AesGcmSecretProtector
from gatescope.adapters.postgres import PostgresPool
from gatescope.formal_auth import FormalIdentityService, FormalSession, LastOwnerRequired
from gatescope.domain.identity import Role
from gatescope.submission_crypto import encrypt_submission_for_test


PASSWORD = "isolated-admin-test-" "password-2026"


def test_admin_lifecycle_isolation_and_encrypted_create(monkeypatch, migrated_database):
    tenant, other, owner, outsider = (uuid4() for _ in range(4))
    encoded = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1).hash(PASSWORD)
    with psycopg.connect(migrated_database) as db:
        # Role is cluster-local to the isolated test instance. It has no RLS bypass.
        db.execute("""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='admin_console_test_runtime')
          THEN CREATE ROLE admin_console_test_runtime LOGIN INHERIT; END IF; END $$""")
        db.execute("GRANT gatescope_api,gatescope_auth TO admin_console_test_runtime")
        for tid, slug, uid, username in ((tenant, "admin-a", owner, "admin-a-owner"), (other, "admin-b", outsider, "admin-b-owner")):
            db.execute("INSERT INTO tenants(id,slug,name,status,created_at,updated_at) VALUES(%s,%s,%s,'ACTIVE',now(),now())", (tid, slug, slug))
            db.execute("""INSERT INTO users(id,normalized_username,display_username,password_hash,status,mfa_required,password_changed_at,created_at,updated_at)
              VALUES(%s,%s,%s,%s,'ACTIVE',true,now(),now(),now())""", (uid, username, username, encoded))
            db.execute("INSERT INTO memberships(id,tenant_id,user_id,role,status,created_at,updated_at) VALUES(%s,%s,%s,'OWNER','ACTIVE',now(),now())", (uuid4(), tid, uid))
            db.execute("INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at) VALUES(%s,%s,%s,%s,now(),now())", (uuid4(), tid, uid, slug))
    parsed = urlsplit(migrated_database)
    runtime_url = urlunsplit((parsed.scheme, "admin_console_test_runtime@"+parsed.netloc.split("@")[-1], parsed.path, parsed.query, ""))
    # Keep migration fixture's environment untouched through teardown.
    monkeypatch.setattr("apps.api.formal_app._database_url", lambda: runtime_url)
    for key, value in {"IDENTITY_KEK": b"k"*32, "RECOVERY_PEPPER": b"p"*32, "CURSOR_SIGNING_KEY": b"c"*32}.items():
        monkeypatch.setenv("GATESCOPE_"+key, base64.urlsafe_b64encode(value).decode())
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEY_VERSION", "admin-test")
    monkeypatch.setenv("GATESCOPE_PUBLIC_ORIGIN", "https://testserver")
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    monkeypatch.setenv("GATESCOPE_SUBMISSION_KEY_VERSION", "admin-test")
    monkeypatch.setenv("GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY", base64.urlsafe_b64encode(private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode())
    monkeypatch.setenv("GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY", base64.urlsafe_b64encode(private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode())
    app = create_formal_app()
    with TestClient(app, base_url="https://testserver") as client:
        paths = ["/api/v2/admin/"+part for part in ("overview", "users", "resources", "audit")]
        for path in paths:
            assert client.get(path).status_code == 401
        assert client.get("/admin/users", follow_redirects=False).status_code == 303
        response = client.post("/auth/login", json={"tenant": "admin-a", "username": "admin-a-owner", "password": PASSWORD})
        assert response.status_code == 200 and response.json()["mfa_required"]
        for path in paths:
            assert client.get(path).status_code == 403
        headers = {"X-CSRF-Token": client.cookies.get("gatescope_csrf")}
        enrollment = client.post("/auth/mfa-methods:enroll", headers=headers, json={"label": "isolated test"})
        assert enrollment.status_code == 200
        code, _ = totp_code(enrollment.json()["secret"])
        assert client.post(f"/auth/mfa-methods/{enrollment.json()['method_id']}:confirm", headers=headers, json={"code": code}).status_code == 200
        for path in paths:
            result = client.get(path)
            assert result.status_code == 200, (path, result.json())
            assert result.headers["Cache-Control"] == "no-store"
            assert "admin-b-owner" not in result.text
        listing = client.get(paths[1]).json()
        assert listing["total"] == 1 and listing["items"][0]["id"] == str(owner)
        assert listing["items"][0]["resources"]["watchlists"] == 1
        with psycopg.connect(migrated_database) as db:
            db.execute("UPDATE users SET locked_until=now()+interval '10 minutes' WHERE id=%s", (owner,))
        locked = client.get(paths[1]+"?status=LOCKED").json()
        assert locked["total"] == 1 and locked["items"][0]["status"] == "LOCKED"
        assert client.get(paths[1]+"?status=ACTIVE").json()["total"] == 0
        assert client.get(paths[0]).json()["members"]["locked"] == 1
        with psycopg.connect(migrated_database) as db:
            db.execute("UPDATE users SET locked_until=NULL WHERE id=%s", (owner,))
        assert client.get(paths[1]+"?page_size=101").status_code == 422
        assert client.post(f"{paths[1]}/{outsider}/sessions:revoke", headers=headers).status_code == 404
        assert client.post(f"{paths[1]}/{owner}/sessions:revoke", headers=headers).status_code == 409

        # Creating a user is a real DB write, but only inside this disposable test.
        payload = {"username": "admin-created-reader", "initial_password": PASSWORD, "role": "VIEWER"}
        def envelope():
            return {"encrypted_submission": encrypt_submission_for_test(private.public_key(), key_version="admin-test", purpose=f"tenant:{tenant}:user_create", request_id=str(uuid4()), issued_at=int(time.time()*1000), payload=payload).model_dump()}
        assert client.post("/api/v2/users", json=envelope()).status_code == 403  # CSRF
        created = client.post("/api/v2/users", headers=headers, json=envelope())
        assert created.status_code == 201, created.json()
        member = created.json()
        assert PASSWORD not in created.text and member["mfa_required"] is True
        assert client.post("/api/v2/users", headers=headers, json=envelope()).status_code == 409
        filtered = client.get(paths[1]+"?q=created-reader&role=VIEWER&page_size=1").json()
        assert filtered["total"] == 1 and filtered["items"][0]["id"] == member["id"]
        member_path = "/api/v2/memberships/"+member["membership"]["id"]
        update_headers = {**headers, "If-Match": '"1"'}
        disabled = client.patch(member_path, headers=update_headers, json={"role": "VIEWER", "status": "REVOKED"})
        assert disabled.status_code == 200, disabled.json()
        assert client.patch(member_path, headers=update_headers, json={"role": "VIEWER", "status": "ACTIVE"}).status_code == 412
        restored = client.patch(member_path, headers={**headers, "If-Match": '"2"'}, json={"role": "RESEARCHER", "status": "ACTIVE"})
        assert restored.status_code == 200
        owner_member = listing["items"][0]["membership"]
        assert client.patch("/api/v2/memberships/"+owner_member["id"], headers={**headers, "If-Match": '"1"'}, json={"role": "VIEWER", "status": "ACTIVE"}).status_code == 409
        assert client.post(f"{paths[1]}/{member['id']}/sessions:revoke", headers=headers).status_code == 200
        audit = client.get(paths[3]).json()
        assert any(row["action"] == "admin.sessions.revoke" for row in audit["items"])
        # A stale password confirmation must be recoverable, not masked as 404.
        with psycopg.connect(migrated_database) as db:
            db.execute("UPDATE sessions SET recent_auth_at=now()-interval '1 hour' WHERE user_id=%s", (owner,))
        stale = client.patch(member_path, headers={**headers, "If-Match": '"3"'}, json={"role": "VIEWER", "status": "ACTIVE"})
        assert stale.status_code == 403 and stale.json()["error"]["code"] == "RECENT_OWNER_AUTH_REQUIRED"
        assert client.post("/auth/reauthenticate", headers=headers, json={"password": PASSWORD}).status_code == 200
        if os.environ.get("RUN_ADMIN_BROWSER") == "1":
            exercise_browser(client)
        # Dynamic roles are re-read from DB: an ordinary member cannot access admin.
        with psycopg.connect(migrated_database) as db:
            db.execute("UPDATE memberships SET role='VIEWER' WHERE tenant_id=%s AND user_id=%s", (tenant, owner))
        for path in paths:
            assert client.get(path).status_code == 403
        assert client.post("/api/v2/users", headers=headers, json=envelope()).status_code == 403
        concurrent_last_owner_check(migrated_database, runtime_url, tenant, encoded)
    # Restricted runtime role cannot see any tenant row without a context.
    with psycopg.connect(make_conninfo(migrated_database, user="admin_console_test_runtime")) as db:
        assert db.execute("SELECT count(*) FROM memberships").fetchone()[0] == 0


def concurrent_last_owner_check(database, runtime_url, tenant, password_hash):
    """Two actual PG transactions race to remove different OWNER memberships."""
    now = datetime.now(timezone.utc)
    actors, memberships = [], []
    with psycopg.connect(database) as db:
        for index in range(2):
            uid, mid, sid = uuid4(), uuid4(), uuid4()
            username = f"concurrent-admin-{index}"
            db.execute("""INSERT INTO users(id,normalized_username,display_username,password_hash,status,mfa_required,password_changed_at,created_at,updated_at)
              VALUES(%s,%s,%s,%s,'ACTIVE',true,now(),now(),now())""", (uid, username, username, password_hash))
            db.execute("INSERT INTO memberships(id,tenant_id,user_id,role,status,created_at,updated_at) VALUES(%s,%s,%s,'OWNER','ACTIVE',now(),now())", (mid, tenant, uid))
            db.execute("""INSERT INTO sessions(id,tenant_id,user_id,token_hash,csrf_hash,authenticated_at,recent_auth_at,expires_at,last_seen_at,created_at,auth_strength,mfa_verified_at)
              VALUES(%s,%s,%s,%s,%s,now(),now(),now()+interval '1 hour',now(),now(),'MFA',now())""", (sid, tenant, uid, str(sid)*2, str(uid)*2))
            actors.append(FormalSession(sid, tenant, uid, username, Role.OWNER, now+timedelta(hours=1), now, now, "MFA", True))
            memberships.append(mid)
    pool = PostgresPool(runtime_url, min_size=2, max_size=3)
    pool.open()
    try:
        service = FormalIdentityService(pool, AesGcmSecretProtector(b"k"*32, "admin-test"), b"p"*32)
        barrier = Barrier(2)
        def demote(index):
            barrier.wait(timeout=5)
            try:
                service.update_membership(actors[index], memberships[index], role=Role.VIEWER, status="ACTIVE", expected_etag=1, trace_id="concurrent-owner-test")
                return "DEMOTED"
            except LastOwnerRequired:
                return "PROTECTED"
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(demote, range(2))) == ["DEMOTED", "PROTECTED"]
        with psycopg.connect(database) as db:
            assert db.execute("SELECT count(*) FROM memberships WHERE tenant_id=%s AND role='OWNER' AND status='ACTIVE'", (tenant,)).fetchone()[0] == 1
    finally:
        pool.close()


def exercise_browser(client):
    """Real browser + actual HTTP handlers and isolated DB; no response fixtures."""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_cookies([{"name": item.name, "value": item.value, "url": "https://testserver", "secure": True} for item in client.cookies.jar])

            def proxy(route):
                request = route.request
                parsed = urlsplit(request.url)
                assert parsed.netloc == "testserver"
                response = client.request(request.method, parsed.path+"?"+parsed.query,
                    content=request.post_data_buffer, headers=dict(request.headers))
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            context.route("**/*", proxy)
            page = context.new_page()
            page.set_default_timeout(10000)
            expect.set_options(timeout=10000)
            page.goto("https://testserver/admin/users", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="用户管理", exact=True)).to_be_visible()
            page.get_by_role("button", name="创建用户", exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.locator('input[name="username"]').fill("browser-created-user")
            dialog.locator('input[name="initial_password"]').fill(PASSWORD)
            dialog.locator('select[name="role"]').select_option("VIEWER")
            dialog.get_by_role("button", name="创建用户", exact=True).click()
            expect(dialog).to_have_count(0)
            row = page.get_by_role("row").filter(has_text="browser-created-user")
            expect(row).to_be_visible()
            member = next(item for item in client.get("/api/v2/admin/users").json()["items"] if item["username"] == "browser-created-user")
            assert member["mfa_required"] is True and member["membership"]["role"] == "VIEWER"
            row.get_by_role("button", name="编辑", exact=True).click()
            page.get_by_role("dialog").locator('select[name="status"]').select_option("REVOKED")
            page.get_by_role("dialog").get_by_role("button", name="保存变更", exact=True).click()
            page.get_by_role("button", name="确认操作", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            expect(row).to_contain_text("已停用")
            member = next(item for item in client.get("/api/v2/admin/users?status=REVOKED").json()["items"] if item["id"] == member["id"])
            assert member["membership"]["status"] == "REVOKED"
            row.get_by_role("button", name="编辑", exact=True).click()
            page.get_by_role("dialog").locator('select[name="status"]').select_option("ACTIVE")
            page.get_by_role("dialog").locator('select[name="role"]').select_option("RESEARCHER")
            page.get_by_role("dialog").get_by_role("button", name="保存变更", exact=True).click()
            page.get_by_role("button", name="确认操作", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            expect(row).to_contain_text("研究员")
            expect(row).to_contain_text("已启用")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
            row.get_by_role("button", name="详情", exact=True).click()
            expect(page.get_by_role("dialog")).to_be_visible()
            page.get_by_role("button", name="关闭弹窗").click()
        finally:
            browser.close()
