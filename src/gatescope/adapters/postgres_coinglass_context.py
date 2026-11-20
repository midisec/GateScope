"""User-scoped provider cache; private material never crosses the read plane."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import required_row
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.domain.coinglass_cache import (
    cached_evidence,
    epoch,
    merge_refresh,
    project_cache,
    project_refresh_state,
)
from gatescope.quant.structure_detection import VERSION

PERIODS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400}


class PostgresCoinglassContextRepository:
    def __init__(self, pool: PostgresPool):
        self.pool = pool

    def provider_wait(self, tenant_id, user_id, config_id, secret_version: int) -> int:
        with self.pool.transaction(tenant_id, user_id) as conn:
            return required_row(conn.execute(
                "SELECT gatescope_security.coinglass_provider_wait(%s,%s,%s,%s)",
                (tenant_id, user_id, config_id, secret_version),
            ).fetchone())[0]

    def pause_provider(self, tenant_id, user_id, config_id, secret_version: int, seconds: int) -> int:
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 2_147_483_647:
            raise ValueError("INVALID_PROVIDER_COOLDOWN")
        if not seconds:
            return self.provider_wait(tenant_id, user_id, config_id, secret_version)
        with self.pool.transaction(tenant_id, user_id) as conn:
            return required_row(conn.execute(
                "SELECT gatescope_security.pause_coinglass_provider(%s,%s,%s,%s,%s)",
                (tenant_id, user_id, config_id, secret_version, seconds),
            ).fetchone())[0]

    def get(self, tenant_id, user_id, instrument_id: UUID, timeframe: str, request: bool = False) -> dict:
        if timeframe not in PERIODS:
            raise ValueError("UNSUPPORTED_TIMEFRAME")
        empty: dict = {
            "version": "coinglass-context-1",
            "instrument_id": str(instrument_id),
            "timeframe": timeframe,
            "scope": None,
            "match": {"status": "UNSUPPORTED", "reason": "NOT_READY"},
            "as_of": None,
            "fetched_at": 0,
            "expires_at": 0,
            "series": [],
            "quality_flags": [],
            "history_scope": "LATEST_WINDOW_256_NOT_FULL_HISTORY",
        }
        with self.pool.transaction(tenant_id, user_id) as conn:
            conn.execute("SET LOCAL statement_timeout='2s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            # MCP intentionally cannot read credential/config rows. Validate cached
            # access via a narrow security-definer function, not a secret-table grant.
            config = conn.execute(
                "SELECT * FROM gatescope_security.coinglass_chart_access(%s,%s)", (tenant_id, user_id)
            ).fetchone()
            if not config:
                return project_refresh_state(
                    project_cache(
                        {**empty, "status": "NO_CONFIG", "reason": "NO_ACTIVE_OWNED_CONFIG"},
                        now=int(datetime.now(timezone.utc).timestamp()),
                        requested=False,
                        due_at=0,
                    ),
                    now=int(datetime.now(timezone.utc).timestamp()),
                    exists=False,
                    eligible=False,
                )
            if request:
                conn.execute(
                    "SELECT gatescope_security.request_coinglass_chart(%s,%s,%s,%s)",
                    (tenant_id, user_id, instrument_id, timeframe),
                )
            row = conn.execute(
                """SELECT payload,config_id,secret_version,due_at,updated_at,requested_at,
                  pending_since,lease_until,collection_started_at,
                  gatescope_security.coinglass_chart_is_watched(tenant_id,user_id,instrument_id,timeframe)
                FROM coinglass_chart_cache
                WHERE tenant_id=%s AND user_id=%s AND instrument_id=%s AND timeframe=%s
                  AND EXISTS(SELECT 1 FROM instruments i WHERE i.id=instrument_id AND i.status='ACTIVE')""",
                (tenant_id, user_id, instrument_id, timeframe),
            ).fetchone()
            wait = required_row(conn.execute(
                "SELECT gatescope_security.coinglass_provider_wait(%s,%s,%s,%s)",
                (tenant_id, user_id, *config),
            ).fetchone())[0]
        now = int(datetime.now(timezone.utc).timestamp())
        valid = bool(row and row[1] == config[0] and row[2] == config[1])
        requested = bool(row is not None and valid and request and row[5] and row[5].timestamp() > now - 5)
        result = row[0] if row is not None and valid and row[0] else {"status": "QUEUED" if requested else "NOT_CACHED"}
        if request and not valid:
            result = {**result, "reason": "CACHE_REQUEST_NOT_QUEUED"}
        result = project_cache(
            {**empty, **result},
            now=now,
            requested=requested,
            due_at=int(row[3].timestamp()) if row is not None and valid else 0,
            last_updated=int(row[4].timestamp()) if row is not None and valid and row[4] else 0,
            provider_retry_at=now + wait if wait else 0,
        )
        pending = int(row[6].timestamp()) if row is not None and valid and row[6] else 0
        eligible = bool(row is not None and valid and (pending or row[9] or (row[5] and row[5].timestamp() > now - 7200)))
        return project_refresh_state(
            result,
            now=now,
            exists=valid,
            eligible=eligible,
            pending_since=pending,
            lease_until=int(row[7].timestamp()) if row is not None and valid and row[7] else 0,
            collection_started_at=int(row[8].timestamp()) if row is not None and valid and row[8] else 0,
        )

    def seed(self):
        with self.pool.public_transaction() as conn:
            conn.execute("SELECT gatescope_security.seed_coinglass_charts()")

    def claim(self):
        with self.pool.public_transaction() as conn:
            row = conn.execute("SELECT * FROM gatescope_security.claim_coinglass_chart()").fetchone()
        if not row:
            return None
        tenant, user, instrument, tf, lease = row
        with self.pool.transaction(tenant, user) as conn:
            config = conn.execute(
                """SELECT c.id,c.secret_version,c.secret_ciphertext,c.secret_nonce,c.wrapped_data_key,
                c.key_version,c.encryption_context FROM coinglass_configs c JOIN coinglass_chart_cache cc ON cc.config_id=c.id
                WHERE cc.tenant_id=%s AND cc.user_id=%s AND cc.instrument_id=%s AND cc.timeframe=%s
                AND c.created_by=%s AND c.status='ACTIVE'""",
                (tenant, user, instrument, tf, user),
            ).fetchone()
            item = conn.execute(
                "SELECT id,venue,market,symbol,base_asset,quote_asset,settle_asset FROM instruments WHERE id=%s",
                (instrument,),
            ).fetchone()
        if not config or not item:
            return None
        return {
            "tenant": tenant,
            "user": user,
            "instrument": dict(
                zip(
                    ("id", "venue", "market", "symbol", "base_asset", "quote_asset", "settle_asset"),
                    item,
                    strict=True,
                )
            ),
            "timeframe": tf,
            "lease": lease,
            "config": config,
        }

    @staticmethod
    def _locked_payload(conn, job):
        identity = (
            job["tenant"],
            job["user"],
            job["instrument"]["id"],
            job["timeframe"],
            job["lease"],
            job["config"][0],
            job["config"][1],
        )
        return conn.execute(
            """SELECT cc.payload FROM coinglass_chart_cache cc
              WHERE cc.tenant_id=%s AND cc.user_id=%s AND cc.instrument_id=%s AND cc.timeframe=%s
                AND cc.lease_id=%s AND cc.config_id=%s AND cc.secret_version=%s
                AND EXISTS(SELECT 1 FROM coinglass_configs c WHERE c.id=cc.config_id AND c.tenant_id=cc.tenant_id
                  AND c.status='ACTIVE' AND c.secret_version=cc.secret_version AND c.created_by=cc.user_id)
              FOR UPDATE OF cc""",
            identity,
        ).fetchone()

    def publish_progress(self, job, bundle):
        """Publish genuine collected metrics while the same job retains its lease."""
        now = int(datetime.now(timezone.utc).timestamp())
        fresh = {
            **bundle,
            "series": [
                item
                for item in bundle.get("series", [])
                if isinstance(item, dict) and item.get("status") in {"READY", "PARTIAL"}
            ],
        }
        if (
            bundle.get("status") != "PARTIAL"
            or bundle.get("instrument_id") != str(job["instrument"]["id"])
            or bundle.get("timeframe") != job["timeframe"]
            or not cached_evidence(fresh, now=now)[0]
        ):
            return False
        with self.pool.transaction(job["tenant"], job["user"]) as conn:
            previous = self._locked_payload(conn, job)
            if previous is None:
                return False
            result = merge_refresh(
                previous[0],
                bundle,
                instrument_id=str(job["instrument"]["id"]),
                timeframe=job["timeframe"],
                now=now,
                ttl=max(180, min(PERIODS[job["timeframe"]], 900)),
            )
            result["refresh_status"] = "COLLECTING"
            from gatescope.domain.coinglass_context import canonical_hash

            result["snapshot_id"] = canonical_hash(
                {key: value for key, value in result.items() if key != "snapshot_id"}
            )
            updated = conn.execute(
                """UPDATE coinglass_chart_cache SET payload=%s,updated_at=now()
              WHERE tenant_id=%s AND user_id=%s AND instrument_id=%s AND timeframe=%s AND lease_id=%s
                AND config_id=%s AND secret_version=%s
                AND EXISTS(SELECT 1 FROM coinglass_configs c WHERE c.id=coinglass_chart_cache.config_id
                  AND c.status='ACTIVE' AND c.secret_version=coinglass_chart_cache.secret_version
                  AND c.created_by=coinglass_chart_cache.user_id)""",
                (
                    Jsonb(result),
                    job["tenant"],
                    job["user"],
                    job["instrument"]["id"],
                    job["timeframe"],
                    job["lease"],
                    *job["config"][:2],
                ),
            )
            return updated.rowcount == 1

    def complete(self, job, bundle, *, delay=300):
        now = int(datetime.now(timezone.utc).timestamp())
        provider = (job["tenant"], job["user"], job["config"][0], job["config"][1])
        with self.pool.transaction(job["tenant"], job["user"]) as conn:
            # A stale lease or rotated credential may neither retain old data nor
            # pause a replacement key. Lock and validate before either write.
            previous = self._locked_payload(conn, job)
            if previous is None:
                return
            wait = required_row(conn.execute(
                "SELECT gatescope_security.coinglass_provider_wait(%s,%s,%s,%s)", provider
            ).fetchone())[0]
            retry = epoch(bundle.get("retry_after_seconds"))
            absolute = epoch(bundle.get("retry_at"))
            if absolute:
                retry = max(0, absolute - now)
            # Hooks normally persisted the pause when 429 arrived. Only fill a
            # missing pause here; never move that original deadline on completion.
            if retry and not wait:
                wait = required_row(conn.execute(
                    "SELECT gatescope_security.pause_coinglass_provider(%s,%s,%s,%s,%s)",
                    (*provider, min(2_147_483_647, retry)),
                ).fetchone())[0]
            result = merge_refresh(
                previous[0],
                bundle,
                instrument_id=str(job["instrument"]["id"]),
                timeframe=job["timeframe"],
                now=now,
                ttl=max(180, min(PERIODS[job["timeframe"]], 900)),
                retry_at=now + wait if wait else 0,
            )
            refresh_delay = max(wait, max(1, min(86400, int(delay))))
            result = project_cache(result, now=now, requested=False, due_at=now + refresh_delay)
            conn.execute(
                """UPDATE coinglass_chart_cache SET payload=%s,secret_version=%s,
              updated_at=now(),due_at=to_timestamp(%s),lease_id=NULL,lease_until=NULL,pending_since=NULL
              WHERE tenant_id=%s AND user_id=%s AND instrument_id=%s AND timeframe=%s AND lease_id=%s
                AND EXISTS(SELECT 1 FROM coinglass_configs c WHERE c.id=coinglass_chart_cache.config_id
                  AND c.status='ACTIVE' AND c.secret_version=%s AND c.created_by=coinglass_chart_cache.user_id)""",
                (
                    Jsonb(result),
                    job["config"][1],
                    now + refresh_delay,
                    job["tenant"],
                    job["user"],
                    job["instrument"]["id"],
                    job["timeframe"],
                    job["lease"],
                    job["config"][1],
                ),
            )

    def defer(self, job, *, delay: int):
        """Release a pacing-blocked job without pretending a refresh happened."""
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 2_147_483_647:
            raise ValueError("INVALID_PROVIDER_DELAY")
        with self.pool.transaction(job["tenant"], job["user"]) as conn:
            conn.execute(
                """UPDATE coinglass_chart_cache cc SET lease_id=NULL,lease_until=NULL,
              due_at=greatest(cc.due_at,now()+make_interval(secs=>%s))
              WHERE cc.tenant_id=%s AND cc.user_id=%s AND cc.instrument_id=%s AND cc.timeframe=%s
                AND cc.lease_id=%s AND cc.config_id=%s AND cc.secret_version=%s
                AND EXISTS(SELECT 1 FROM coinglass_configs c WHERE c.id=cc.config_id AND c.tenant_id=cc.tenant_id
                  AND c.status='ACTIVE' AND c.secret_version=cc.secret_version AND c.created_by=cc.user_id)""",
                (
                    delay,
                    job["tenant"],
                    job["user"],
                    job["instrument"]["id"],
                    job["timeframe"],
                    job["lease"],
                    *job["config"][:2],
                ),
            )

    def review(self, tenant, user, instrument, timeframe, context, candidate, expected=None, scenario=None):
        from gatescope.domain.coinglass_context import build_review

        if context != context_id(instrument, timeframe):
            raise ValueError("CURRENT_CONTEXT_REQUIRED")
        bundle = self.get(tenant, user, instrument, timeframe, request=True)
        with self.pool.transaction(tenant, user) as conn:
            row = conn.execute(
                """SELECT checkpoint_json->'records'->%s, (checkpoint_json->>'last_ts')::bigint
              FROM market_pattern_tracking WHERE instrument_id=%s AND timeframe=%s AND version=%s""",
                (candidate, instrument, timeframe, VERSION),
            ).fetchone()
            if not row or not row[0]:
                raise LookupError("PATTERN_NOT_CURRENT")
            record = row[0]
            event = {**record["latest"], "context_id": context}
            changed = bool(expected and event.get("evidence_id") != expected)
            if changed and scenario is not None:
                raise ValueError("PATTERN_EVIDENCE_CHANGED")
            if event.get("phase") != "FORMING":
                raise ValueError("PATTERN_NO_LONGER_FORMING")
            now = int(datetime.now(timezone.utc).timestamp())
            result = build_review(event, bundle, assessed_at=now, decision_bar_time=row[1], scenario=scenario)
            result.update(evidence_changed=changed, requested_evidence_id=expected)
            result["bundle"] = bundle
            review_id = uuid4()
            conn.execute(
                """INSERT INTO coinglass_pattern_reviews(id,tenant_id,user_id,instrument_id,timeframe,context_id,candidate_id,payload)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (review_id, tenant, user, instrument, timeframe, context, candidate, Jsonb(result)),
            )
        return {"review_id": str(review_id), "review": result}

    def review_detail(self, tenant, user, review_id):
        with self.pool.transaction(tenant, user) as conn:
            row = conn.execute(
                "SELECT payload FROM coinglass_pattern_reviews WHERE id=%s", (review_id,)
            ).fetchone()
        return {"review_id": str(review_id), "review": row[0]} if row else None
