"""Annual indicator evidence: immutable compressed chunks, bounded API pages.

Interrupted runs retain their frozen inputs and restart deterministic indicator
calculation from that prefix, never pretending an indicator-state checkpoint
exists. Temporary calculation arrays spill to disk instead of worker RAM.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.quant.artifacts import report_digest
from gatescope.quant.backtest import ENGINE_VERSION, StrategySpec, run_backtest
from gatescope.quant.research_sequences import CHUNK_ROWS, ResearchSpool, bar_document, decode_chunk, document_bar, encode_chunk


class IndicatorResearchRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def _save(self, tenant: UUID, user: UUID, run: UUID, kind: str, index: int,
              count: int, payload: bytes, digest: str) -> None:
        with self.pool.transaction(tenant, user) as conn:
            conn.execute("""INSERT INTO research_indicator_chunks
                (run_id,tenant_id,owner_user_id,kind,chunk_index,row_count,payload,digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (run, tenant, user, kind, index, count, payload, digest))
            found = conn.execute("SELECT digest,row_count FROM research_indicator_chunks WHERE run_id=%s AND kind=%s AND chunk_index=%s",
                (run, kind, index)).fetchone()
            if found != (digest, count):
                raise ValueError("RESEARCH_IMMUTABLE_CHUNK_CONFLICT")

    def _progress(self, tenant: UUID, user: UUID, run: UUID, status: dict[str, Any]) -> None:
        with self.pool.transaction(tenant, user) as conn:
            conn.execute("""UPDATE research_backtests SET progress_json=%s,
                lease_until=now()+interval '5 minutes' WHERE id=%s AND state='RUNNING'""", (Jsonb(status), run))

    def run(self, request: dict[str, Any], tenant: UUID, user: UUID, spec: StrategySpec,
            *, trials: int, progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        from gatescope.adapters.postgres_backtests import BacktestRepository
        repository = BacktestRepository(self.pool)
        run = request["id"]
        previous = request.get("progress") or {}
        if previous.get("engine_version") not in (None, ENGINE_VERSION):
            raise ValueError("回测引擎已升级，请新建回测；原冻结数据已保留")
        last_heartbeat = 0.0
        status: dict[str, Any] = {"phase": "FREEZING_INPUTS", "engine_version": ENGINE_VERSION,
            "processed_bars": 0, "total_bars": request["bar_limit"],
            "restart_policy": "REPLAY_FROZEN_INPUTS", "input_complete": bool(previous.get("input_complete"))}

        def notify(done: int, total: int, *, force: bool = False) -> None:
            nonlocal last_heartbeat
            now = time.monotonic()
            if not force and now - last_heartbeat < 5:
                return
            status.update(processed_bars=done, total_bars=total)
            self._progress(tenant, user, run, status)
            if progress:
                progress(done, total)
            last_heartbeat = now

        with ResearchSpool() as spool:
            bars = spool.sequence("bars", bars=True)
            # Recover only immutable facts, with a bounded chunk query each time.
            chunk_index = 0
            while True:
                with self.pool.transaction(tenant, user) as conn:
                    row = conn.execute("SELECT payload,digest,row_count FROM research_indicator_chunks WHERE run_id=%s AND kind='inputs' AND chunk_index=%s",
                        (run, chunk_index)).fetchone()
                if row is None:
                    break
                documents = decode_chunk(row[0], row[1])
                if len(documents) != row[2]:
                    raise ValueError("RESEARCH_CHUNK_INVALID")
                for item in documents:
                    bars.append(document_bar(item))
                chunk_index += 1
                notify(len(bars), request["bar_limit"])
            bounds = previous.get("input_bounds")
            if not bounds:
                bounds = repository.input_bounds(request["instrument_id"], request["timeframe"], request["bar_limit"],
                    request["created_at"], request["from_time"], request["to_time"])
            status["input_bounds"] = bounds
            total = bounds["available_bars"]
            if total < 300:
                raise ValueError("需要 300–550000 根已收盘 K 线")
            if bounds["internal_missing_bars"]:
                raise ValueError("所选历史 K 线不连续，请缩短区间或补齐行情")
            if not status["input_complete"]:
                # Freeze data chunk before publishing its progress. Resuming a
                # final partial chunk never appends a new chunk to it.
                after = bars[-1].open_time if bars else None
                for batch, manifest in repository.iter_bar_batches(request["instrument_id"], request["timeframe"],
                        request["created_at"], bounds, after=after):
                    documents = [{**bar_document(bar), "source_hash": source["hash"]} for bar, source in zip(batch, manifest, strict=True)]
                    payload, digest = encode_chunk(documents)
                    self._save(tenant, user, run, "inputs", chunk_index, len(documents), payload, digest)
                    chunk_index += 1
                    for bar in batch:
                        bars.append(bar)
                    notify(len(bars), total)
                status["input_complete"] = True
            if len(bars) != total:
                raise ValueError("冻结行情数量不一致，请新建回测；原记录已保留")
            if spec.direction != "LONG" and bars[0].market == "SPOT":
                raise ValueError("现货回测只支持做多")
            status["phase"] = "CALCULATING"
            notify(0, len(bars), force=True)
            report = run_backtest(bars, spec, tested_hypotheses=trials,
                progress=lambda done, count: notify(done, count), sequence_factory=spool.sequence)
            status["phase"] = "SAVING_EVIDENCE"
            for name, kind in (("signals", "signals"), ("full_trades", "trades"), ("full_curve", "curve"), ("drawdown", "drawdown")):
                for index, count, payload, digest in spool.sequences[name].chunks():
                    self._save(tenant, user, run, kind, index, count, payload, digest)
                    notify(len(bars), len(bars))
            report["cache_coverage"] = bounds
            if bounds["missing_bars"]:
                report["quality_flags"] = sorted(set(report["quality_flags"]) | {"INCOMPLETE_REQUESTED_HISTORY"})
                report["evidence"]["reasons"].append("本地可用历史短于请求范围；报告按实际覆盖计算，未补造缺失行情")
            report["detail_storage"].update(base_url=f"/api/v2/research/backtests/{run}/indicator",
                restart_policy="REPLAY_FROZEN_INPUTS", encoding="zlib-json-sha256")
            # Bind summary to every immutable chunk without repeating contents.
            with self.pool.transaction(tenant, user) as conn:
                chunks = conn.execute("SELECT kind,chunk_index,row_count,digest FROM research_indicator_chunks WHERE run_id=%s ORDER BY kind,chunk_index", (run,)).fetchall()
            report["detail_storage"]["manifest"] = [dict(zip(("kind", "chunk_index", "row_count", "digest"), row, strict=True)) for row in chunks]
            report["artifact_digest"] = report_digest(report)
            status["phase"] = "COMPLETE"
            notify(len(bars), len(bars), force=True)
            return report

    def page(self, tenant: UUID, user: UUID, run: UUID, *, kind: str, offset: int, limit: int) -> dict[str, Any]:
        if kind not in {"inputs", "signals", "trades", "curve", "drawdown"}:
            raise ValueError("未知的研究证据类型")
        with self.pool.transaction(tenant, user) as conn:
            found = conn.execute("SELECT report_json->'detail_storage'->'counts' FROM research_backtest_results WHERE run_id=%s", (run,)).fetchone()
            if not found or not found[0]:
                raise LookupError("年度指标报告不存在或尚未完成")
            total = int(found[0][kind])
            end = min(total, offset + limit)
            rows = conn.execute("SELECT chunk_index,payload,digest FROM research_indicator_chunks WHERE run_id=%s AND kind=%s AND chunk_index>=%s AND chunk_index<=%s ORDER BY chunk_index",
                (run, kind, offset // CHUNK_ROWS, max(offset, end - 1) // CHUNK_ROWS)).fetchall() if offset < total else []
        items = []
        for index, payload, digest in rows:
            data = decode_chunk(payload, digest)
            items.extend(data[max(0, offset-index*CHUNK_ROWS):min(len(data), end-index*CHUNK_ROWS)])
        if len(items) != max(0, end-offset):
            raise ValueError("RESEARCH_EVIDENCE_PAGE_INCOMPLETE")
        return {"items": items, "offset": offset, "total": total, "has_more": end < total,
            "next_offset": end if end < total else None}

    def chart(self, tenant: UUID, user: UUID, run: UUID, *, offset: int | None,
              limit: int, trade_index: int | None = None) -> dict[str, Any]:
        from gatescope.adapters.postgres_backtests import BacktestRepository
        report = BacktestRepository(self.pool).get(tenant, user, run)["report"]
        if not report or not report.get("detail_storage"):
            raise LookupError("年度指标报告不存在或尚未完成")
        selected = None
        if trade_index is not None:
            records = self.page(tenant, user, run, kind="trades", offset=trade_index, limit=1)
            if not records["items"]:
                raise LookupError("交易不存在")
            selected = records["items"][0]
            # Report duration and exact count define the contiguous input step;
            # no nearest-candle snapping is used.
            first = datetime.fromisoformat(report["range"]["from"])
            step = (datetime.fromisoformat(report["range"]["to"])-first).total_seconds()/report["bar_count"]
            offset = max(0, int((datetime.fromisoformat(selected["entry_time"])-first).total_seconds()/step)-30)
            limit = min(2000, max(limit, int(selected.get("holding_bars", 1))+60))
        offset = max(0, report["bar_count"]-limit) if offset is None else offset
        inputs = self.page(tenant, user, run, kind="inputs", offset=offset, limit=limit)
        signals = self.page(tenant, user, run, kind="signals", offset=offset, limit=limit)
        warmup = report["detail_storage"]["warmup_bars"]
        curve_start = max(0, offset-warmup+1)
        curve_end = max(0, min(report["bar_count"], offset+limit)-warmup+1)
        curve = self.page(tenant, user, run, kind="drawdown", offset=curve_start, limit=max(1, curve_end-curve_start))
        return {"offset": offset, "total": report["bar_count"], "has_more": inputs["has_more"], "next_offset": inputs["next_offset"],
            "selected_trade": selected, "candles": [{"time": b["open_time"], **{key: float(b[key]) for key in ("open", "high", "low", "close")}} for b in inputs["items"]],
            "input_bars": inputs["items"], "indicator_series": signals["items"],
            "equity_curve": curve["items"] if curve_end > curve_start else []}
