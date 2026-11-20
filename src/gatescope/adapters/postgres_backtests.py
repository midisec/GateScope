"""Research queue and frozen market/report snapshots."""
from __future__ import annotations

import builtins

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import required_row
from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.quant.backtest import MAX_INDICATOR_BARS, StrategySpec, run_backtest
from gatescope.observability import event


class BacktestRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def create(self, tenant: UUID, user: UUID, instrument: UUID, timeframe: str, spec: StrategySpec,
               limit: int, start: datetime | None, end: datetime | None) -> dict[str, Any]:
        identifier = uuid4()
        with self.pool.transaction(tenant, user) as conn:
            conn.execute("""INSERT INTO research_backtests
              (id,tenant_id,owner_user_id,instrument_id,timeframe,spec_json,bar_limit,from_time,to_time,state)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED')""",
              (identifier, tenant, user, instrument, timeframe, Jsonb(spec.model_dump()), limit, start, end))
        return self.get(tenant, user, identifier)

    def list(self, tenant: UUID, user: UUID, *, limit: int = 50, offset: int = 0,
             instrument: UUID | None = None, timeframe: str = "", screening: str = "", engine_mode: str = "") -> builtins.list[dict[str, Any]]:
        with self.pool.transaction(tenant, user) as conn:
            rows = conn.execute(self._select() + " WHERE (%s::uuid IS NULL OR r.instrument_id=%s) AND (%s='' OR r.timeframe=%s) AND (%s='' OR coalesce(j.report_json->'screening'->>'status','UNASSESSED')=%s) AND (%s='' OR coalesce(r.spec_json->>'engine_mode','INDICATOR')=%s) ORDER BY r.created_at DESC,r.id DESC LIMIT %s OFFSET %s",
                (instrument, instrument, timeframe, timeframe, screening, screening, engine_mode, engine_mode, max(1, min(limit, 101)), max(0, offset))).fetchall()
        return [self._row(row) for row in rows]

    def count(self, tenant: UUID, user: UUID, *, instrument: UUID | None = None, timeframe: str = "", screening: str = "", engine_mode: str = "") -> int:
        with self.pool.transaction(tenant, user) as conn:
            return int(required_row(conn.execute("SELECT count(*) FROM research_backtests r LEFT JOIN research_backtest_results j ON j.run_id=r.id WHERE (%s::uuid IS NULL OR r.instrument_id=%s) AND (%s='' OR r.timeframe=%s) AND (%s='' OR coalesce(j.report_json->'screening'->>'status','UNASSESSED')=%s) AND (%s='' OR coalesce(r.spec_json->>'engine_mode','INDICATOR')=%s)", (instrument, instrument, timeframe, timeframe, screening, screening, engine_mode, engine_mode)).fetchone())[0])

    def get(self, tenant: UUID, user: UUID, identifier: UUID) -> dict[str, Any]:
        with self.pool.transaction(tenant, user) as conn:
            row = conn.execute(self._select() + " WHERE r.id=%s", (identifier,)).fetchone()
            if row is None:
                raise LookupError("回测不存在")
            report = conn.execute("SELECT report_json FROM research_backtest_results WHERE run_id=%s", (identifier,)).fetchone()
        return {**self._row(row), "report": report[0] if report else None}

    def coverage(self, instrument: UUID, timeframe: str, *, limit: int = MAX_INDICATOR_BARS,
                 start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
        """Actual requested window, not a misleading latest-5000 preview."""
        bounds = self.input_bounds(instrument, timeframe, limit, datetime.now(timezone.utc), start, end)
        return {**bounds, "limit": limit, "from_time": bounds.get("available_from"), "to_time": bounds.get("available_to")}

    def input_bounds(self, instrument: UUID, timeframe: str, limit: int, as_of: datetime,
                     start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
        step = TIMEFRAME_SECONDS[timeframe]
        limit = max(1, min(limit, MAX_INDICATOR_BARS))
        with self.pool.public_transaction() as conn:
            latest = conn.execute("""SELECT open_time FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                AND available_at<=%s AND close_time<=%s AND created_at<=%s
                AND (%s::timestamptz IS NULL OR close_time<=%s)
                AND (%s::timestamptz IS NULL OR open_time>=%s)
                ORDER BY open_time DESC LIMIT 1""", (instrument,timeframe,as_of,as_of,as_of,end,end,start,start)).fetchone()
            if latest is None:
                return {"available_bars": 0, "requested_bars": limit, "missing_bars": limit,
                    "internal_missing_bars": 0, "delayed_bars": 0, "ratio": 0,
                    "from": None, "to": None, "available_from": None, "available_to": None}
            right = latest[0]
            left = right-timedelta(seconds=(limit-1)*step)
            if start:
                left = max(left, start)
            row = conn.execute("""WITH snapshot AS (
                SELECT DISTINCT ON(open_time) open_time,close_time,available_at FROM bars
                WHERE instrument_id=%s AND timeframe=%s AND is_final
                  AND available_at<=%s AND close_time<=%s AND created_at<=%s
                  AND open_time>=%s AND open_time<=%s ORDER BY open_time,revision DESC)
                SELECT count(*),min(open_time),max(close_time),
                  count(*) FILTER(WHERE available_at-close_time>=close_time-open_time) FROM snapshot""",
                (instrument,timeframe,as_of,as_of,as_of,left,right)).fetchone()
        count, first, last, delayed = required_row(row)
        expected = int((right-left).total_seconds()//step)+1
        internal = max(0, int((last-first).total_seconds()/step)-count) if count else 0
        return {"available_bars": count, "requested_bars": expected, "missing_bars": max(0,expected-count),
            "internal_missing_bars": internal, "delayed_bars": delayed, "ratio": count/expected if expected else None,
            "from": left.isoformat(), "to": (right+timedelta(seconds=step)).isoformat(),
            "available_from": first.isoformat() if first else None, "available_to": last.isoformat() if last else None,
            "truncated_by_limit": bool(start and (right-start).total_seconds()/step+1>limit)}

    def iter_bar_batches(self, instrument: UUID, timeframe: str, as_of: datetime,
                         bounds: dict[str, Any], *, after: datetime | None = None):
        """Short keyset queries keep yearly input off the worker heap."""
        from gatescope.quant.research_sequences import CHUNK_ROWS
        while True:
            with self.pool.public_transaction() as conn:
                rows = conn.execute("""WITH next_bar AS MATERIALIZED (
                    SELECT open_time FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                      AND available_at<=%s AND close_time<=%s AND created_at<=%s
                      AND open_time>=%s AND close_time<=%s
                      AND (%s::timestamptz IS NULL OR open_time>%s)
                      ORDER BY open_time ASC LIMIT 1)
                    SELECT snapshot.* FROM next_bar n CROSS JOIN LATERAL (
                    SELECT DISTINCT ON(open_time) venue,market,instrument_id,timeframe,open_time,close_time,
                    open,high,low,close,volume,is_final,revision,available_at,quality_flags,source_sequence,payload_hash
                    FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                    AND available_at<=%s AND close_time<=%s AND created_at<=%s
                    AND open_time>=n.open_time AND open_time<n.open_time+make_interval(secs=>%s)
                    AND close_time<=%s ORDER BY open_time,revision DESC LIMIT %s
                    ) snapshot ORDER BY snapshot.open_time""",
                    (instrument,timeframe,as_of,as_of,as_of,bounds["from"],bounds["to"],after,after,
                     instrument,timeframe,as_of,as_of,as_of,CHUNK_ROWS*TIMEFRAME_SECONDS[timeframe],bounds["to"],CHUNK_ROWS)).fetchall()
            if not rows:
                return
            yield [self._bar(row) for row in rows], [{"open_time": r[4].isoformat(), "revision": r[12], "hash": r[16]} for r in rows]
            after = rows[-1][4]

    @staticmethod
    def _select() -> str:
        return """SELECT r.id,i.symbol,r.instrument_id,r.timeframe,r.state,r.spec_json,r.bar_limit,
             r.from_time,r.to_time,r.created_at,r.completed_at,r.safe_error,
             j.report_json->'full'->'metrics',j.report_json->'bar_count',j.report_json->'validation',j.report_json->'screening',r.progress_json
             FROM research_backtests r JOIN instruments i ON i.id=r.instrument_id
             LEFT JOIN research_backtest_results j ON j.run_id=r.id"""

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return dict(zip(("id", "symbol", "instrument_id", "timeframe", "state", "spec", "bar_limit", "from_time", "to_time", "created_at", "completed_at", "error", "metrics", "bar_count", "validation", "screening", "progress"), row, strict=True))

    def bars(self, instrument: UUID, timeframe: str, limit: int, as_of: datetime,
             start: datetime | None = None, end: datetime | None = None,
             *, source_manifest: builtins.list[dict[str, Any]] | None = None) -> builtins.list[Bar]:
        if limit > 5000:
            raise ValueError("长历史回测需要分片存储，不能一次加载完整报告")
        with self.pool.public_transaction() as conn:
            rows = conn.execute("""SELECT * FROM (
              SELECT DISTINCT ON (open_time) venue,market,instrument_id,timeframe,open_time,close_time,
                open,high,low,close,volume,is_final,revision,available_at,quality_flags,source_sequence,payload_hash
              FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                AND available_at<=%s AND close_time<=%s AND created_at<=%s
                AND (%s::timestamptz IS NULL OR open_time >= %s)
                AND (%s::timestamptz IS NULL OR close_time <= %s)
              ORDER BY open_time DESC,revision DESC LIMIT %s
            ) snapshot ORDER BY open_time""", (instrument, timeframe, as_of, as_of, as_of, start, start, end, end, limit)).fetchall()
        if source_manifest is not None:
            source_manifest.extend({"open_time": r[4].isoformat(), "revision": r[12], "hash": r[16]} for r in rows)
        # PostgreSQL pads numeric scale; reconstructed hashes differ from ingestion
        # hashes in that case. Preserve both instead of pretending they are equal.
        return [self._bar(r) for r in rows]

    @staticmethod
    def _bar(r: Any) -> Bar:
        return Bar(venue=r[0], market=r[1], instrument_id=str(r[2]), timeframe=r[3], open_time=r[4], close_time=r[5], open=r[6], high=r[7], low=r[8], close=r[9], volume=r[10], is_final=r[11], revision=r[12], available_at=r[13], quality_flags=frozenset(r[14]), source_sequence=r[15])

    def run_one(self, *, progress: Callable[[int, int], None] | None = None) -> bool:
        with self.pool.public_transaction() as conn:
            claimed = conn.execute("SELECT * FROM gatescope_security.claim_research_backtest()").fetchone()
        if not claimed:
            return False
        identifier, tenant, user = claimed
        try:
            request = self.get(tenant, user, identifier)
            spec = StrategySpec.model_validate(request["spec"])
            with self.pool.transaction(tenant, user) as conn:
                trials = required_row(conn.execute("SELECT count(*) FROM research_backtests WHERE created_at<=%s", (request["created_at"],)).fetchone())[0]
                trials += required_row(conn.execute("SELECT coalesce(sum(trial_count),0) FROM research_discovery_reports WHERE created_at<=%s", (request["created_at"],)).fetchone())[0]
            if request["bar_limit"] > 5000:
                from gatescope.adapters.postgres_indicator_research import IndicatorResearchRepository
                report = IndicatorResearchRepository(self.pool).run(request, tenant, user, spec,
                    trials=max(1,trials), progress=progress)
            else:
                source_manifest: builtins.list[dict[str, Any]] = []
                bars = self.bars(request["instrument_id"], request["timeframe"], request["bar_limit"], request["created_at"], request["from_time"], request["to_time"], source_manifest=source_manifest)
                if spec.direction != "LONG" and bars and bars[0].market == "SPOT":
                    raise ValueError("现货回测只支持做多")
                report = run_backtest(bars, spec, tested_hypotheses=max(1, trials), source_manifest=source_manifest, progress=progress)
            with self.pool.transaction(tenant, user) as conn:
                conn.execute("INSERT INTO research_backtest_results (run_id,tenant_id,owner_user_id,report_json) VALUES (%s,%s,%s,%s)", (identifier, tenant, user, Jsonb(report)))
                conn.execute("UPDATE research_backtests SET state='SUCCEEDED',completed_at=now() WHERE id=%s", (identifier,))
        except Exception as exc:
            safe = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "需要 300–550000 根已收盘 K 线", "所选历史 K 线不连续，请缩短区间或补齐行情",
                "样本不足以分割验证区间，请增加 K 线数量或减少持仓期限", "现货回测只支持做多",
                "形态回测超过 120 秒计算预算，请减少形态或样本数量",
                "包含形态扫描的组合最多 5000 根；年度形态请使用同源形态回测",
                "回测引擎已升级，请新建回测；原冻结数据已保留",
                "冻结行情数量不一致，请新建回测；原记录已保留",
            } else "回测计算失败，请调整区间后重试"
            event("backtest.failed", level=40, error_type=type(exc).__name__)
            with self.pool.transaction(tenant, user) as conn:
                conn.execute("UPDATE research_backtests SET state='FAILED',safe_error=%s,completed_at=now() WHERE id=%s", (safe, identifier))
        return True
