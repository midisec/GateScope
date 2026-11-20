from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.research import build_research_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.quant.artifacts import report_digest
from tests.integration.test_postgres_rls import seed_two_tenants, TENANT_A, TENANT_B, USER_A, USER_B


def test_discoveries_are_owner_isolated_immutable_and_count_all_trials(migrated_database):
    seed_two_tenants(migrated_database)
    api = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_strategy"))
    api.open()
    worker.open()
    identifier = uuid4()
    document = {
        "kind": "DIRECTIONAL",
        "candidates": [],
        "deployable_count": 0,
        "value": -0.0,
        "details": {"model-1": {"trades": [], "private_evidence": True}},
    }
    try:
        with worker.transaction(TENANT_A, USER_A) as conn:
            conn.execute(
                "INSERT INTO research_discovery_reports(id,tenant_id,owner_user_id,name,trial_count,report_json,artifact_digest) VALUES (%s,%s,%s,'test',2376,%s,%s)",
                (identifier, TENANT_A, USER_A, Jsonb(document), report_digest(document)),
            )
        app = FastAPI()
        app.include_router(build_research_router(api))
        identity = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A, role=SimpleNamespace(value="OWNER"))

        @app.middleware("http")
        async def auth(request: Request, call_next):
            request.state.auth_session = identity
            return await call_next(request)

        with TestClient(app) as client:
            assert client.get("/api/v2/research/discoveries").json()["total"] == 1
            report = client.get(f"/api/v2/research/discoveries/{identifier}")
            assert report.status_code == 200 and report.headers["Cache-Control"] == "no-store"
            assert report.json()["artifact_digest"] == report_digest(report.json())
            assert "details" not in report.json()
            assert report.json()["source_artifact_digest"] == report_digest(document)
            full = client.get(f"/api/v2/research/discoveries/{identifier}?include_details=true").json()
            assert full["artifact_digest"] == report_digest(full) == report_digest(document)
            assert "details" in full
            detail = client.get(f"/api/v2/research/discoveries/{identifier}/directional/model-1")
            assert detail.status_code == 200 and detail.json()["artifact_digest"] == report_digest(
                detail.json()
            )
            assert (
                client.get(f"/api/v2/research/discoveries/{identifier}/directional/unknown").status_code
                == 404
            )
            assert client.get("/api/v2/research/discoveries?offset=10").json()["items"] == []
            assert client.get("/api/v2/research/discoveries?offset=-1").status_code == 422
            for tenant, user in ((TENANT_A, USER_B), (TENANT_B, USER_B)):
                identity.tenant_id = tenant
                identity.user_id = user
                assert client.get("/api/v2/research/discoveries").json()["items"] == []
                assert client.get(f"/api/v2/research/discoveries/{identifier}").status_code == 404
                assert (
                    client.get(f"/api/v2/research/discoveries/{identifier}?include_details=true").status_code
                    == 404
                )
                assert (
                    client.get(f"/api/v2/research/discoveries/{identifier}/directional/model-1").status_code
                    == 404
                )
        with worker.transaction(TENANT_A, USER_A) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE research_discovery_reports SET name='changed' WHERE id=%s", (identifier,)
                )
    finally:
        api.close()
        worker.close()
