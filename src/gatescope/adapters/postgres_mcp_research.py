"""Strictly read-only, explicitly owner-bound exports of existing research evidence."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from gatescope.adapters.postgres import required_row
from gatescope.adapters.postgres import PostgresPool, validated_uuid
from gatescope.domain.market import TIMEFRAME_SECONDS

VERSION = "mcp-research-1"
SECTIONS = frozenset({"summary", "trades", "equity", "validation", "cases", "events", "inputs"})
MAX_PAGE_BYTES = 512_000


def _json_ready(value):
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    return value


def _bounds(limit, offset, *, maximum=100):
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= maximum
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 1_000_000
    ):
        raise ValueError("INVALID_RESEARCH_PAGINATION")


def _page(items, total, limit, offset):
    selected: list[dict] = []
    size = 0
    budget_limited = False
    for item in items[:limit]:
        ready = _json_ready(item)
        encoded = len(json.dumps(ready, ensure_ascii=False, allow_nan=False).encode())
        if size + encoded > MAX_PAGE_BYTES:
            if not selected:
                raise ValueError("RESEARCH_EVIDENCE_ITEM_EXCEEDS_EXPORT_BUDGET")
            budget_limited = True
            break
        selected.append(ready)
        size += encoded
    more = offset + len(selected) < total
    return {
        "version": VERSION,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "source": "EXISTING_LOCAL_DATABASE",
        "items": selected,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(selected),
        "has_more": more,
        "next_offset": offset + len(selected) if more else None,
        "truncated": more,
        "truncation_reason": "OUTPUT_BYTE_BUDGET" if budget_limited else "PAGE_LIMIT" if more else None,
    }


_RUN_SUMMARY = """SELECT jsonb_build_object(
  'run_id',r.id,'instrument_id',r.instrument_id,'symbol',i.symbol,'venue',i.venue,'market',i.market,
  'timeframe',r.timeframe,'state',r.state,'engine_mode',coalesce(r.spec_json->>'engine_mode','INDICATOR'),
  'created_at',r.created_at,'started_at',r.started_at,'completed_at',r.completed_at,
  'requested_from',r.from_time,'requested_to',r.to_time,'requested_bar_limit',r.bar_limit,
  'safe_error',r.safe_error,'progress',r.progress_json,'report_available',j.run_id IS NOT NULL,
  'engine_version',j.report_json->'engine_version','detector_version',j.report_json->'detector_version',
  'artifact_digest',j.report_json->'artifact_digest','artifact_digest_method',j.report_json->'artifact_digest_method',
  'bar_count',j.report_json->'bar_count','range',j.report_json->'range',
  'chunked_details',j.report_json ? 'detail_storage','detail_counts',j.report_json#>'{detail_storage,counts}',
  'cache_coverage',j.report_json->'cache_coverage',
  'metrics',j.report_json#>'{full,metrics}','training',j.report_json->'training',
  'validation',j.report_json->'validation','screening',j.report_json->'screening',
  'cost_model',jsonb_build_object('fee_bps',r.spec_json->'fee_bps','slippage_bps',r.spec_json->'slippage_bps',
    'funding_bps_per_8h',r.spec_json->'funding_bps_per_8h','initial_equity',r.spec_json->'initial_equity'),
  'availability',j.report_json->'availability','evidence',j.report_json->'evidence')
  FROM research_backtests r JOIN instruments i ON i.id=r.instrument_id
  LEFT JOIN research_backtest_results j ON j.run_id=r.id AND j.tenant_id=r.tenant_id AND j.owner_user_id=r.owner_user_id
  WHERE r.tenant_id=%s AND r.owner_user_id=%s"""


class McpResearchRepository:
    def __init__(self, pool: PostgresPool):
        self.pool = pool

    @staticmethod
    def _timeouts(conn):
        conn.execute("SET LOCAL statement_timeout='2s'")
        conn.execute("SET LOCAL lock_timeout='1s'")

    def watchlist(self, tenant, user, *, limit=50, offset=0):
        _bounds(limit, offset)
        with self.pool.read_transaction(tenant, user) as conn:
            self._timeouts(conn)
            owned = """FROM watchlist_items wi JOIN watchlists w ON w.id=wi.watchlist_id AND w.tenant_id=wi.tenant_id
              JOIN instruments i ON i.id=wi.instrument_id
              WHERE w.tenant_id=%s AND w.owner_user_id=%s AND wi.tenant_id=%s AND w.deleted_at IS NULL"""
            total = required_row(conn.execute("SELECT count(*) " + owned, (tenant, user, tenant)).fetchone())[0]
            rows = conn.execute(
                """SELECT jsonb_build_object('watchlist_id',w.id,'watchlist_name',w.name,
              'position',wi.position,'instrument_id',i.id,'symbol',i.symbol,'venue',i.venue,'market',i.market,
              'instrument_status',i.status,'base',i.base_asset,'quote',i.quote_asset,'settle',i.settle_asset,
              'default_timeframe',wi.default_timeframe,'cache',(
                SELECT jsonb_build_object('active',s.active,'history_status',s.history_status,
                  'cached_bars',s.cached_bars,'target_bars',s.target_bars,'missing_bars',s.missing_bars,
                  'first_open_time',s.first_open_time,'last_close_time',s.last_close_time,
                  'last_live_at',s.last_live_at,'last_history_at',s.last_history_at,
                  'has_cached_history',s.cached_bars>0,'stale',s.last_live_at IS NULL OR s.last_live_at<now()-interval '45 seconds')
                FROM market_sync_state s WHERE s.instrument_id=i.id AND s.timeframe=wi.default_timeframe)) """
                + owned
                + " ORDER BY w.created_at,w.id,wi.position,wi.id LIMIT %s OFFSET %s",
                (tenant, user, tenant, limit, offset),
            ).fetchall()
        result = _page([row[0] for row in rows], total, limit, offset)
        result.update(kind="WATCHLIST", cache_policy="READ_EXISTING_ONLY_NO_SUBSCRIPTION_OR_PROVIDER_REQUEST")
        return result

    def backtests(self, tenant, user, *, instrument=None, timeframe=None, limit=20, offset=0):
        _bounds(limit, offset, maximum=50)
        instrument = validated_uuid(instrument) if instrument is not None else None
        if timeframe is not None and (not isinstance(timeframe, str) or timeframe not in TIMEFRAME_SECONDS):
            raise ValueError("UNSUPPORTED_TIMEFRAME")
        filtered = " AND (%s::uuid IS NULL OR r.instrument_id=%s) AND (%s::text IS NULL OR r.timeframe=%s)"
        args = (tenant, user, instrument, instrument, timeframe, timeframe)
        with self.pool.read_transaction(tenant, user) as conn:
            self._timeouts(conn)
            total = required_row(conn.execute(
                "SELECT count(*) FROM research_backtests r WHERE r.tenant_id=%s AND r.owner_user_id=%s"
                + filtered,
                args,
            ).fetchone())[0]
            rows = conn.execute(
                _RUN_SUMMARY + filtered + " ORDER BY r.created_at DESC,r.id DESC LIMIT %s OFFSET %s",
                (*args, limit, offset),
            ).fetchall()
        return {
            **_page([row[0] for row in rows], total, limit, offset),
            "kind": "BACKTEST_RUNS",
            "execution_policy": "EXISTING_RUNS_ONLY_NO_CREATE_OR_RUN",
        }

    @staticmethod
    def _report_page(conn, tenant, user, run_id, path, limit, offset):
        # The database selects just this page, not an entire 5,000-bar JSON report.
        row = conn.execute(
            """WITH saved AS MATERIALIZED (
          SELECT CASE WHEN jsonb_typeof(j.report_json#>%s::text[])='array'
            THEN j.report_json#>%s::text[] ELSE '[]'::jsonb END AS items
          FROM research_backtest_results j JOIN research_backtests r ON r.id=j.run_id
            AND r.tenant_id=j.tenant_id AND r.owner_user_id=j.owner_user_id
          WHERE r.id=%s AND r.tenant_id=%s AND r.owner_user_id=%s
            AND j.tenant_id=%s AND j.owner_user_id=%s)
          SELECT jsonb_array_length(items),coalesce((SELECT jsonb_agg(items->n ORDER BY n)
            FROM generate_series(%s,least(jsonb_array_length(items)-1,%s)) n),'[]'::jsonb)
          FROM saved""",
            (path, path, run_id, tenant, user, tenant, user, offset, offset + limit - 1),
        ).fetchone()
        return (row[1], row[0]) if row else ([], 0)

    @staticmethod
    def _table_page(conn, tenant, user, run_id, section, limit, offset):
        table, order, value = {
            "inputs": (
                "research_structure_inputs",
                "bar_index",
                "jsonb_build_object('bar_index',bar_index,'open_time',open_time,'bar',bar_json)",
            ),
            "events": (
                "research_structure_events",
                "observed_time,candidate_id,sequence",
                "jsonb_build_object('candidate_id',candidate_id,'sequence',sequence,'observed_time',observed_time,'phase',phase,'pattern',pattern,'event',event_json)",
            ),
            "cases": (
                "research_structure_cases",
                "observed_time,candidate_id",
                "jsonb_build_object('candidate_id',candidate_id,'pattern',pattern,'observed_time',observed_time,'selected',selected,'baseline',baseline,'state',state,'entry_time',entry_time,'exit_time',exit_time,'net_return',net_return,'fees',fees,'funding',funding,'selection_reasons',selection_reasons,'environment',environment,'case',case_json)",
            ),
            "trades": (
                "research_structure_cases",
                "entry_time,candidate_id",
                "jsonb_build_object('candidate_id',candidate_id,'pattern',pattern,'state',state,'entry_time',entry_time,'exit_time',exit_time,'net_return',net_return,'fees',fees,'funding',funding,'case',case_json)",
            ),
        }[section]
        where = " WHERE run_id=%s AND tenant_id=%s AND owner_user_id=%s"
        if section == "trades":
            where += " AND selected AND state IN ('TARGET','STOP','TIMEOUT')"
        args = (run_id, tenant, user)
        total = required_row(conn.execute(f"SELECT count(*) FROM {table}" + where, args).fetchone())[0]
        rows = conn.execute(
            f"SELECT {value} FROM {table}" + where + f" ORDER BY {order} LIMIT %s OFFSET %s",
            (*args, limit, offset),
        ).fetchall()
        return [row[0] for row in rows], total

    @staticmethod
    def _indicator_chunk_page(conn,tenant,user,run_id,kind,total,limit,offset):
        from gatescope.quant.research_sequences import CHUNK_ROWS,decode_chunk
        end=min(total,offset+limit)
        rows=conn.execute("""SELECT chunk_index,payload,digest FROM research_indicator_chunks
            WHERE run_id=%s AND tenant_id=%s AND owner_user_id=%s AND kind=%s
              AND chunk_index>=%s AND chunk_index<=%s ORDER BY chunk_index""",
            (run_id,tenant,user,kind,offset//CHUNK_ROWS,max(offset,end-1)//CHUNK_ROWS)).fetchall() if offset<total else []
        items=[]
        for index,payload,digest in rows:
            decoded=decode_chunk(payload,digest)
            items.extend(decoded[max(0,offset-index*CHUNK_ROWS):min(len(decoded),end-index*CHUNK_ROWS)])
        if len(items)!=max(0,end-offset):
            raise ValueError("RESEARCH_EVIDENCE_PAGE_INCOMPLETE")
        return items,total

    def backtest_detail(self, tenant, user, run_id, *, section="summary", limit=50, offset=0):
        _bounds(limit, offset)
        run_id = validated_uuid(run_id)
        if not isinstance(section, str) or section not in SECTIONS:
            raise ValueError("INVALID_RESEARCH_SECTION")
        with self.pool.read_transaction(tenant, user) as conn:
            self._timeouts(conn)
            row = conn.execute(_RUN_SUMMARY + " AND r.id=%s", (tenant, user, run_id)).fetchone()
            if row is None:
                raise LookupError("RESEARCH_NOT_FOUND")
            run = row[0]
            structure = run["engine_mode"] == "STRUCTURE"
            available, basis = "AVAILABLE", "SAVED_BACKTEST_REPORT"
            if section == "summary":
                saved = conn.execute(
                    """SELECT jsonb_build_object('spec',r.spec_json,
                  'metrics',j.report_json#>'{full,metrics}','training',j.report_json->'training',
                  'validation',j.report_json->'validation','range',j.report_json->'range',
                  'evidence',j.report_json->'evidence','probability',j.report_json->'probability',
                  'availability',j.report_json->'availability','quality_flags',j.report_json->'quality_flags',
                  'cache_coverage',j.report_json->'cache_coverage','detail_storage',(j.report_json->'detail_storage')-'manifest',
                  'input_digest',j.report_json->'input_digest','input_digest_method',j.report_json->'input_digest_method',
                  'indicator_versions',j.report_json->'indicator_versions','time_convention',j.report_json->'time_convention',
                  'structure_summary',j.report_json#>'{structure_research,summary}',
                  'baseline',j.report_json#>'{structure_research,baseline}',
                  'cost_stress',j.report_json#>'{structure_research,cost_stress}',
                  'cost_stress_basis',j.report_json#>'{structure_research,cost_stress_basis}',
                  'portfolio_metrics_available',j.report_json#>'{structure_research,portfolio_metrics_available}',
                  'portfolio_note',j.report_json#>'{structure_research,portfolio_note}')
                  FROM research_backtests r JOIN research_backtest_results j ON j.run_id=r.id
                    AND j.tenant_id=r.tenant_id AND j.owner_user_id=r.owner_user_id
                  WHERE r.id=%s AND r.tenant_id=%s AND r.owner_user_id=%s
                    AND j.tenant_id=%s AND j.owner_user_id=%s""",
                    (run_id, tenant, user, tenant, user),
                ).fetchone()
                items, total = ([saved[0]] if saved and offset == 0 else [], int(saved is not None))
            elif structure and section in {"cases", "trades", "events", "inputs"}:
                items, total = self._table_page(conn, tenant, user, run_id, section, limit, offset)
                basis = (
                    "FROZEN_STRUCTURE_INPUTS_OR_TRANSITIONS"
                    if section in {"inputs", "events"}
                    else "SAVED_STRUCTURE_CASES"
                )
            elif (structure and section == "equity") or (not structure and section == "cases"):
                items, total, available = [], 0, "NOT_APPLICABLE"
            elif not structure and run.get("chunked_details") and section in {"trades","equity","inputs"}:
                kind={"trades":"trades","equity":"curve","inputs":"inputs"}[section]
                items,total=self._indicator_chunk_page(conn,tenant,user,run_id,kind,
                    int(run["detail_counts"][kind]),limit,offset)
                basis="FROZEN_INDICATOR_CHUNKS_SHA256"
            elif section == "validation" and not structure:
                items, total = (
                    [
                        {
                            "training": run.get("training"),
                            "validation": run.get("validation"),
                            "range": run.get("range"),
                            "evidence": run.get("evidence"),
                            "basis": "TIME_SPLIT_WITH_EMBARGO_NOT_SEALED_HOLDOUT",
                        }
                    ]
                    if run["report_available"] and offset == 0
                    else [],
                    int(run["report_available"]),
                )
            else:
                path = {
                    "trades": ["full", "trades"],
                    "equity": ["full", "equity_curve"],
                    "events": ["pattern_events"],
                    "inputs": ["input_bars"],
                    "validation": ["structure_research", "walk_forward", "folds"],
                }[section]
                items, total = self._report_page(conn, tenant, user, run_id, path, limit, offset)
            if not run["report_available"] and not (
                structure and section in {"cases", "trades", "events", "inputs"}
            ):
                available = "REPORT_NOT_READY"
        result = _page(items, total, limit, offset)
        result.update(
            kind="BACKTEST_DETAIL",
            section=section,
            run=_json_ready(run),
            availability=available,
            evidence_basis=basis,
            immutable=bool(run["report_available"] or (structure and section in {"inputs", "events"})),
            partial_run=run["state"] != "SUCCEEDED",
            research_only=True,
            portfolio_returns_additive=False if structure else None,
        )
        return result
