#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-symbol, multi-timeframe opportunity scanner with local backtests."""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Optional, Sequence

from database import Database
from gate_client import GateClient, GateError, INTERVAL_SECONDS, MAX_HISTORY_POINTS
from pattern_engine import CATALOG_BY_ID, scan_patterns


DEFAULT_OPPORTUNITY_SETTINGS: dict[str, Any] = {
    "watchlist_ids": [],
    "intervals": ["15m", "1h", "4h", "1d"],
    "patterns": [],
    "min_confidence": 0.60,
    "min_win_rate": 0.55,
    "min_samples": 12,
    "confirmed_only": False,
    "active_bars": 5,
    "max_bars": 5000,
    "horizons": [5, 10, 20, 50],
    "auto_enabled": False,
    "auto_seconds": 900,
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    units = ((86400, "天"), (3600, "小时"), (60, "分钟"))
    for unit, label in units:
        if seconds >= unit:
            value = seconds / unit
            return f"{value:.1f}{label}" if abs(value - round(value)) > 1e-9 else f"{int(round(value))}{label}"
    return f"{seconds}秒"


class OpportunityScanner:
    def __init__(self, db: Database, gate: GateClient, intervals: Sequence[str]) -> None:
        self.db = db
        self.gate = gate
        self.allowed_intervals = set(intervals)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._pending = False
        self._running = False
        self._last_auto_started = 0
        self.status: dict[str, Any] = {
            "running": False,
            "run_id": None,
            "started_at": None,
            "finished_at": None,
            "total_tasks": 0,
            "completed_tasks": 0,
            "current_label": "",
            "last_error": None,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gate-opportunity-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def get_settings(self) -> dict[str, Any]:
        stored = self.db.get_setting("opportunity_settings", {})
        settings = dict(DEFAULT_OPPORTUNITY_SETTINGS)
        if isinstance(stored, dict):
            settings.update(stored)
        settings["watchlist_ids"] = [int(v) for v in settings.get("watchlist_ids", [])]
        settings["intervals"] = [v for v in settings.get("intervals", []) if v in self.allowed_intervals]
        settings["patterns"] = [v for v in settings.get("patterns", []) if v in CATALOG_BY_ID]
        settings["horizons"] = sorted({int(v) for v in settings.get("horizons", []) if int(v) > 0})
        if not settings["intervals"]:
            settings["intervals"] = list(DEFAULT_OPPORTUNITY_SETTINGS["intervals"])
        if not settings["horizons"]:
            settings["horizons"] = list(DEFAULT_OPPORTUNITY_SETTINGS["horizons"])
        return settings

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(DEFAULT_OPPORTUNITY_SETTINGS)
        normalized.update(settings)
        normalized["watchlist_ids"] = sorted({int(v) for v in normalized.get("watchlist_ids", [])})
        normalized["intervals"] = [v for v in normalized.get("intervals", []) if v in self.allowed_intervals]
        normalized["patterns"] = [v for v in normalized.get("patterns", []) if v in CATALOG_BY_ID]
        normalized["horizons"] = sorted({int(v) for v in normalized.get("horizons", []) if int(v) > 0})
        normalized["min_confidence"] = clamp(normalized.get("min_confidence", 0.60))
        normalized["min_win_rate"] = clamp(normalized.get("min_win_rate", 0.55))
        normalized["min_samples"] = max(1, min(500, int(normalized.get("min_samples", 12))))
        normalized["active_bars"] = max(1, min(100, int(normalized.get("active_bars", 5))))
        normalized["max_bars"] = max(300, min(MAX_HISTORY_POINTS - 20, int(normalized.get("max_bars", 5000))))
        normalized["auto_seconds"] = max(300, min(86400, int(normalized.get("auto_seconds", 900))))
        normalized["confirmed_only"] = bool(normalized.get("confirmed_only"))
        normalized["auto_enabled"] = bool(normalized.get("auto_enabled"))
        if not normalized["intervals"]:
            normalized["intervals"] = list(DEFAULT_OPPORTUNITY_SETTINGS["intervals"])
        if not normalized["horizons"]:
            normalized["horizons"] = list(DEFAULT_OPPORTUNITY_SETTINGS["horizons"])
        self.db.set_setting("opportunity_settings", normalized)
        self._wake.set()
        return normalized

    def request_run(self) -> dict[str, Any]:
        with self._lock:
            if self._running or self._pending:
                return dict(self.status)
            self._pending = True
            self.status.update({"running": True, "current_label": "等待后台任务", "last_error": None})
        self._wake.set()
        return dict(self.status)

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self.get_settings()
            now = int(time.time())
            should_auto = bool(settings.get("auto_enabled")) and (
                now - self._last_auto_started >= int(settings.get("auto_seconds", 900))
            )
            with self._lock:
                should_run = self._pending or (should_auto and not self._running)
                if should_run:
                    self._pending = False
                    self._running = True
            if should_run:
                if should_auto:
                    self._last_auto_started = now
                try:
                    self._execute(settings)
                except Exception as exc:
                    self.status.update({"last_error": str(exc), "finished_at": int(time.time())})
                    run_id = self.status.get("run_id")
                    if run_id:
                        self.db.finish_opportunity_run(int(run_id), "failed", str(exc))
                finally:
                    with self._lock:
                        self._running = False
                    self.status["running"] = False
            wait_seconds = 5
            if settings.get("auto_enabled"):
                elapsed = int(time.time()) - self._last_auto_started
                wait_seconds = max(2, min(30, int(settings.get("auto_seconds", 900)) - elapsed))
            self._wake.wait(wait_seconds)
            self._wake.clear()

    def _selected_watchlist(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        all_items = [row for row in self.db.list_watchlist() if row.get("enabled")]
        selected_ids = set(int(v) for v in settings.get("watchlist_ids", []))
        if not selected_ids:
            return all_items
        return [row for row in all_items if int(row["id"]) in selected_ids]

    def _execute(self, settings: dict[str, Any]) -> None:
        watchlist = self._selected_watchlist(settings)
        intervals = list(settings["intervals"])
        tasks = [(item, interval) for item in watchlist for interval in intervals]
        run_id = self.db.create_opportunity_run(settings, len(tasks))
        self.status.update({
            "running": True,
            "run_id": run_id,
            "started_at": int(time.time()),
            "finished_at": None,
            "total_tasks": len(tasks),
            "completed_tasks": 0,
            "current_label": "准备扫描",
            "last_error": None,
        })
        if not tasks:
            self.db.finish_opportunity_run(run_id, "completed", None)
            self.status.update({"running": False, "finished_at": int(time.time()), "current_label": "没有可扫描的自选"})
            return

        for index, (watch, interval) in enumerate(tasks, start=1):
            if self._stop.is_set():
                self.db.finish_opportunity_run(run_id, "cancelled", "服务正在停止")
                return
            label = f"{watch['symbol']} · {interval.upper()}"
            self.status["current_label"] = label
            self.db.update_opportunity_run_progress(run_id, index - 1, label)
            try:
                task, results = self._scan_task(run_id, watch, interval, settings)
                result_ids = self.db.replace_opportunity_task(run_id, task, results)
                task["result_ids"] = result_ids
            except Exception as exc:
                self.db.replace_opportunity_task(
                    run_id,
                    {
                        "instrument_id": watch["instrument_id"],
                        "interval": interval,
                        "status": "error",
                        "signals_count": 0,
                        "actionable_count": 0,
                        "best_event_key": None,
                        "latest_price": None,
                        "candles_count": 0,
                        "error": str(exc),
                    },
                    [],
                )
            self.status["completed_tasks"] = index
            self.db.update_opportunity_run_progress(run_id, index, label)

        self.db.finish_opportunity_run(run_id, "completed", None)
        self.status.update({
            "running": False,
            "finished_at": int(time.time()),
            "completed_tasks": len(tasks),
            "current_label": "扫描完成",
            "last_error": None,
        })

    def _ensure_history(self, instrument: dict[str, Any], interval: str, max_bars: int) -> list[dict[str, Any]]:
        step = INTERVAL_SECONDS[interval]
        now = int(time.time())
        desired = min(MAX_HISTORY_POINTS - 20, max(300, int(max_bars)))
        start = now - desired * step
        local_min, local_max = self.db.candle_bounds(instrument["id"], interval)
        try:
            if local_min is None or local_max is None:
                rows, _ = self.gate.fetch_candles(instrument, interval, start, now)
                self.db.upsert_candles(instrument["id"], interval, rows)
            else:
                if start < int(local_min) - step:
                    rows, _ = self.gate.fetch_candles(instrument, interval, start, int(local_min) - step)
                    self.db.upsert_candles(instrument["id"], interval, rows)
                if now > int(local_max):
                    rows = self.gate.fetch_latest(instrument, interval, int(local_max), overlap_bars=3)
                    self.db.upsert_candles(instrument["id"], interval, rows)
        except GateError:
            cached = self.db.latest_candles(instrument["id"], interval, desired)
            if len(cached) < 60:
                raise
            return cached
        return self.db.latest_candles(instrument["id"], interval, desired)

    @staticmethod
    def _pattern_stats(performance: dict[str, Any], horizon: int, pattern_id: str) -> Optional[dict[str, Any]]:
        horizon_payload = (performance.get("by_horizon") or {}).get(str(horizon)) or {}
        for row in horizon_payload.get("by_pattern") or []:
            if row.get("pattern") == pattern_id:
                return row
        return None

    @staticmethod
    def _history_quality(stats: dict[str, Any], min_samples: int) -> float:
        samples = int(stats.get("samples") or 0)
        win_rate = float(stats.get("win_rate") or 0.0)
        ci_low = float(stats.get("win_rate_ci_low") or 0.0)
        avg_return = float(stats.get("avg_signed_return") or 0.0)
        sample_score = clamp(samples / max(50.0, float(min_samples) * 2.0))
        return_score = clamp(0.5 + avg_return / 0.08)
        return clamp(0.38 * win_rate + 0.32 * ci_low + 0.18 * sample_score + 0.12 * return_score)

    def _recommended_horizon(
        self,
        performance: dict[str, Any],
        pattern_id: str,
        horizons: Sequence[int],
        min_samples: int,
    ) -> tuple[Optional[int], Optional[dict[str, Any]], float, list[dict[str, Any]]]:
        candidates: list[dict[str, Any]] = []
        for horizon in horizons:
            stats = self._pattern_stats(performance, int(horizon), pattern_id)
            if not stats:
                continue
            quality = self._history_quality(stats, min_samples)
            item = dict(stats)
            item["horizon"] = int(horizon)
            item["quality_score"] = round(quality, 4)
            candidates.append(item)
        if not candidates:
            return None, None, 0.0, []
        eligible = [row for row in candidates if int(row.get("samples") or 0) >= min_samples]
        pool = eligible or candidates
        best = max(pool, key=lambda row: (row["quality_score"], int(row.get("samples") or 0)))
        return int(best["horizon"]), best, float(best["quality_score"]), candidates

    def _scan_task(
        self,
        run_id: int,
        watch: dict[str, Any],
        interval: str,
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        instrument = self.db.get_instrument(watch["instrument_id"])
        if not instrument:
            raise RuntimeError("交易标的已不存在")
        candles = self._ensure_history(instrument, interval, int(settings["max_bars"]))
        if len(candles) < 60:
            raise RuntimeError(f"历史K线不足，仅有 {len(candles)} 根")
        patterns = settings.get("patterns") or list(CATALOG_BY_ID)
        result = scan_patterns(
            candles,
            selected_patterns=patterns,
            min_confidence=float(settings["min_confidence"]),
            confirmed_only=False,
            max_bars=int(settings["max_bars"]),
            max_events_per_pattern=12,
            include_statistics=True,
            performance_horizons=tuple(settings["horizons"]),
        )
        latest_ts = int(candles[-1]["ts"])
        latest_price = float(candles[-1]["close"])
        step = INTERVAL_SECONDS[interval]
        times = [int(row["ts"]) for row in candles]
        index_by_time = {ts: idx for idx, ts in enumerate(times)}
        latest_by_pattern: dict[str, dict[str, Any]] = {}
        for event in result.get("events") or []:
            direction = str(event.get("signal_direction") or event.get("direction") or "neutral")
            if direction not in {"bullish", "bearish"}:
                continue
            existing = latest_by_pattern.get(event["pattern"])
            if existing is None or (int(event["end_time"]), float(event["confidence"])) > (
                int(existing["end_time"]), float(existing["confidence"])
            ):
                latest_by_pattern[event["pattern"]] = event

        active: list[dict[str, Any]] = []
        for event in latest_by_pattern.values():
            end_index = index_by_time.get(int(event["end_time"]))
            start_index = index_by_time.get(int(event["start_time"]))
            if end_index is None:
                age_bars = max(0, int(round((latest_ts - int(event["end_time"])) / step)))
            else:
                age_bars = max(0, len(candles) - 1 - end_index)
            if age_bars > int(settings["active_bars"]):
                continue
            duration_bars = max(1, (end_index - start_index + 1) if end_index is not None and start_index is not None else int(round((int(event["end_time"]) - int(event["start_time"])) / step)) + 1)
            horizon, stats, history_quality, horizon_stats = self._recommended_horizon(
                result.get("performance") or {},
                event["pattern"],
                settings["horizons"],
                int(settings["min_samples"]),
            )
            samples = int((stats or {}).get("samples") or 0)
            win_rate = (stats or {}).get("win_rate")
            avg_return = (stats or {}).get("avg_signed_return")
            confirmed_pass = bool(event.get("confirmed")) or not bool(settings.get("confirmed_only"))
            historical_pass = (
                stats is not None
                and samples >= int(settings["min_samples"])
                and float(win_rate or 0.0) >= float(settings["min_win_rate"])
                and float(avg_return or 0.0) > 0.0
            )
            actionable = confirmed_pass and historical_pass
            confidence = float(event.get("confidence") or 0.0)
            score = clamp(0.44 * confidence + 0.46 * history_quality + 0.10 * float(bool(event.get("confirmed"))))
            status = "actionable" if actionable else "insufficient" if stats is None or samples < int(settings["min_samples"]) else "watch"
            active.append({
                "run_id": run_id,
                "instrument_id": instrument["id"],
                "interval": interval,
                "event_key": str(event["id"]),
                "pattern": event["pattern"],
                "name": event["name"],
                "direction": str(event.get("signal_direction") or event.get("direction")),
                "start_time": int(event["start_time"]),
                "end_time": int(event["end_time"]),
                "confidence": confidence,
                "confirmed": bool(event.get("confirmed")),
                "age_bars": age_bars,
                "duration_bars": duration_bars,
                "duration_seconds": duration_bars * step,
                "duration_label": human_duration(duration_bars * step),
                "recommended_horizon": horizon,
                "holding_seconds": int(horizon * step) if horizon else None,
                "holding_label": human_duration(int(horizon * step)) if horizon else "--",
                "samples": samples,
                "wins": int((stats or {}).get("wins") or 0),
                "win_rate": win_rate,
                "ci_low": (stats or {}).get("win_rate_ci_low"),
                "ci_high": (stats or {}).get("win_rate_ci_high"),
                "avg_signed_return": avg_return,
                "avg_mfe": (stats or {}).get("avg_mfe"),
                "avg_mae": (stats or {}).get("avg_mae"),
                "history_quality": history_quality,
                "score": score,
                "actionable": actionable,
                "status": status,
                "latest_price": latest_price,
                "event": event,
                "horizon_stats": horizon_stats,
            })

        active.sort(key=lambda row: (bool(row["actionable"]), float(row["score"]), float(row["confidence"])), reverse=True)
        actionable_count = sum(1 for row in active if row["actionable"])
        best = active[0] if active else None
        task_status = "actionable" if actionable_count else "watch" if active else "no_signal"
        task = {
            "instrument_id": instrument["id"],
            "interval": interval,
            "status": task_status,
            "signals_count": len(active),
            "actionable_count": actionable_count,
            "best_event_key": best["event_key"] if best else None,
            "latest_price": latest_price,
            "candles_count": len(candles),
            "error": None,
        }
        return task, active
