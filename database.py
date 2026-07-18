#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite persistence for Gate chart pattern scanner."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    settle TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    base TEXT NOT NULL DEFAULT '',
                    quote TEXT NOT NULL DEFAULT '',
                    is_stock INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_instruments_search
                ON instruments (market, settle, symbol, display_name, is_stock);

                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id TEXT NOT NULL UNIQUE,
                    default_interval TEXT NOT NULL DEFAULT '15m',
                    refresh_seconds INTEGER NOT NULL DEFAULT 15,
                    selected_patterns_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS candles (
                    instrument_id TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (instrument_id, interval, ts),
                    FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_candles_lookup
                ON candles (instrument_id, interval, ts);

                CREATE TABLE IF NOT EXISTS alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    patterns_json TEXT NOT NULL,
                    match_mode TEXT NOT NULL DEFAULT 'any',
                    min_confidence REAL NOT NULL DEFAULT 0.65,
                    confirmed_only INTEGER NOT NULL DEFAULT 0,
                    lookback_bars INTEGER NOT NULL DEFAULT 500,
                    coincidence_bars INTEGER NOT NULL DEFAULT 5,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 1800,
                    browser_notify INTEGER NOT NULL DEFAULT 1,
                    sound INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_triggered_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled
                ON alert_rules (enabled, instrument_id, interval);

                CREATE TABLE IF NOT EXISTS alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    instrument_id TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    pattern_ids_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    triggered_at INTEGER NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (rule_id) REFERENCES alert_rules(id) ON DELETE CASCADE,
                    FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_alert_events_recent
                ON alert_events (triggered_at DESC, is_read);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opportunity_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    total_tasks INTEGER NOT NULL DEFAULT 0,
                    completed_tasks INTEGER NOT NULL DEFAULT 0,
                    current_label TEXT NOT NULL DEFAULT '',
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_opportunity_runs_recent
                ON opportunity_runs (id DESC, status);

                CREATE TABLE IF NOT EXISTS opportunity_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    instrument_id TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signals_count INTEGER NOT NULL DEFAULT 0,
                    actionable_count INTEGER NOT NULL DEFAULT 0,
                    best_event_key TEXT,
                    latest_price REAL,
                    candles_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(run_id, instrument_id, interval),
                    FOREIGN KEY (run_id) REFERENCES opportunity_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_opportunity_tasks_run
                ON opportunity_tasks (run_id, status, actionable_count DESC);

                CREATE TABLE IF NOT EXISTS opportunity_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    instrument_id TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    age_bars INTEGER NOT NULL DEFAULT 0,
                    duration_bars INTEGER NOT NULL DEFAULT 0,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    recommended_horizon INTEGER,
                    holding_seconds INTEGER,
                    samples INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    win_rate REAL,
                    ci_low REAL,
                    ci_high REAL,
                    avg_signed_return REAL,
                    avg_mfe REAL,
                    avg_mae REAL,
                    history_quality REAL NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    actionable INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    latest_price REAL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    UNIQUE(run_id, instrument_id, interval, event_key),
                    FOREIGN KEY (run_id) REFERENCES opportunity_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_opportunity_results_run
                ON opportunity_results (run_id, actionable DESC, score DESC);
                """
            )

    @staticmethod
    def _dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def upsert_instruments(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = int(time.time())
        values = []
        for row in rows:
            values.append(
                (
                    row["id"], row["market"], row.get("settle", ""), row["symbol"],
                    row.get("display_name") or row["symbol"], row.get("base", ""),
                    row.get("quote", ""), int(bool(row.get("is_stock"))),
                    row.get("status", ""), json.dumps(row.get("metadata", {}), ensure_ascii=False), now,
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO instruments
                (id, market, settle, symbol, display_name, base, quote, is_stock, status, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    market=excluded.market,
                    settle=excluded.settle,
                    symbol=excluded.symbol,
                    display_name=excluded.display_name,
                    base=excluded.base,
                    quote=excluded.quote,
                    is_stock=excluded.is_stock,
                    status=excluded.status,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def get_instrument(self, instrument_id: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        item = self._dict(row)
        if item:
            item["is_stock"] = bool(item["is_stock"])
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    def search_instruments(
        self,
        query: str = "",
        market: str = "all",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if market == "stock":
            clauses.append("is_stock=1")
        elif market != "all":
            if market.startswith("futures:"):
                clauses.append("market='futures' AND settle=?")
                params.append(market.split(":", 1)[1])
            else:
                clauses.append("market=?")
                params.append(market)
        if query.strip():
            needle = f"%{query.strip().upper()}%"
            clauses.append("(UPPER(symbol) LIKE ? OR UPPER(display_name) LIKE ? OR UPPER(base) LIKE ?)")
            params.extend([needle, needle, needle])
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        sql = f"""
            SELECT * FROM instruments
            WHERE {' AND '.join(clauses)}
            ORDER BY is_stock DESC, market, symbol
            LIMIT ? OFFSET ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_stock"] = bool(item["is_stock"])
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result.append(item)
        return result

    def instrument_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])

    def add_watchlist(
        self,
        instrument_id: str,
        default_interval: str,
        refresh_seconds: int,
        selected_patterns: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        patterns = json.dumps(selected_patterns or [], ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist
                (instrument_id, default_interval, refresh_seconds, selected_patterns_json, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(instrument_id) DO UPDATE SET
                    default_interval=excluded.default_interval,
                    refresh_seconds=excluded.refresh_seconds,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (instrument_id, default_interval, refresh_seconds, patterns, now, now),
            )
        item = self.get_watchlist_by_instrument(instrument_id)
        if item is None:
            raise RuntimeError("自选创建失败")
        return item

    def get_watchlist_by_instrument(self, instrument_id: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT w.*, i.market, i.settle, i.symbol, i.display_name, i.base, i.quote, i.is_stock
                FROM watchlist w JOIN instruments i ON i.id=w.instrument_id
                WHERE w.instrument_id=?
                """,
                (instrument_id,),
            ).fetchone()
        return self._normalize_watchlist(row)

    def list_watchlist(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT w.*, i.market, i.settle, i.symbol, i.display_name, i.base, i.quote, i.is_stock,
                       (SELECT c.close FROM candles c
                        WHERE c.instrument_id=w.instrument_id AND c.interval=w.default_interval
                        ORDER BY c.ts DESC LIMIT 1) AS last_price,
                       (SELECT c.ts FROM candles c
                        WHERE c.instrument_id=w.instrument_id AND c.interval=w.default_interval
                        ORDER BY c.ts DESC LIMIT 1) AS last_ts
                FROM watchlist w JOIN instruments i ON i.id=w.instrument_id
                ORDER BY w.created_at DESC
                """
            ).fetchall()
        return [self._normalize_watchlist(row) for row in rows if row is not None]

    def _normalize_watchlist(self, row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["is_stock"] = bool(item["is_stock"])
        item["selected_patterns"] = json.loads(item.pop("selected_patterns_json") or "[]")
        return item

    def update_watchlist(self, watchlist_id: int, **fields: Any) -> Optional[dict[str, Any]]:
        allowed = {"default_interval", "refresh_seconds", "enabled", "selected_patterns_json"}
        updates = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key}=?")
            if key == "selected_patterns_json" and not isinstance(value, str):
                value = json.dumps(value or [], ensure_ascii=False)
            if key == "enabled":
                value = int(bool(value))
            params.append(value)
        if updates:
            updates.append("updated_at=?")
            params.append(int(time.time()))
            params.append(watchlist_id)
            with self.connect() as conn:
                conn.execute(f"UPDATE watchlist SET {', '.join(updates)} WHERE id=?", params)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT w.*, i.market, i.settle, i.symbol, i.display_name, i.base, i.quote, i.is_stock
                FROM watchlist w JOIN instruments i ON i.id=w.instrument_id WHERE w.id=?
                """,
                (watchlist_id,),
            ).fetchone()
        return self._normalize_watchlist(row)

    def delete_watchlist(self, watchlist_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE id=?", (watchlist_id,))

    def upsert_candles(
        self,
        instrument_id: str,
        interval: str,
        rows: Sequence[tuple[int, float, float, float, float, float]],
    ) -> int:
        if not rows:
            return 0
        now = int(time.time())
        values = [
            (instrument_id, interval, int(ts), float(o), float(h), float(l), float(c), float(v), now)
            for ts, o, h, l, c, v in rows
        ]
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO candles
                (instrument_id, interval, ts, open, high, low, close, volume, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, interval, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def candle_bounds(self, instrument_id: str, interval: str) -> tuple[Optional[int], Optional[int]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM candles WHERE instrument_id=? AND interval=?",
                (instrument_id, interval),
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def get_candles(
        self,
        instrument_id: str,
        interval: str,
        start_ts: Optional[int],
        end_ts: Optional[int],
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        clauses = ["instrument_id=?", "interval=?"]
        params: list[Any] = [instrument_id, interval]
        if start_ts is not None:
            clauses.append("ts>=?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("ts<=?")
            params.append(end_ts)
        params.append(max(1, min(limit, 20000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT ts, open, high, low, close, volume
                FROM candles WHERE {' AND '.join(clauses)}
                ORDER BY ts ASC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_candles(self, instrument_id: str, interval: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, open, high, low, close, volume FROM candles
                WHERE instrument_id=? AND interval=? ORDER BY ts DESC LIMIT ?
                """,
                (instrument_id, interval, max(1, min(limit, 5000))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_alert_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alert_rules
                (name, instrument_id, interval, patterns_json, match_mode, min_confidence,
                 confirmed_only, lookback_bars, coincidence_bars, cooldown_seconds,
                 browser_notify, sound, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"], payload["instrument_id"], payload["interval"],
                    json.dumps(payload["patterns"], ensure_ascii=False), payload.get("match_mode", "any"),
                    float(payload.get("min_confidence", 0.65)), int(bool(payload.get("confirmed_only"))),
                    int(payload.get("lookback_bars", 500)), int(payload.get("coincidence_bars", 5)),
                    int(payload.get("cooldown_seconds", 1800)), int(bool(payload.get("browser_notify", True))),
                    int(bool(payload.get("sound", True))), int(bool(payload.get("enabled", True))), now, now,
                ),
            )
            rule_id = int(cur.lastrowid)
        rule = self.get_alert_rule(rule_id)
        if rule is None:
            raise RuntimeError("告警规则创建失败")
        return rule

    def _normalize_rule(self, row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        item = dict(row)
        item["patterns"] = json.loads(item.pop("patterns_json") or "[]")
        for key in ("confirmed_only", "browser_notify", "sound", "enabled"):
            item[key] = bool(item[key])
        return item

    def get_alert_rule(self, rule_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, i.symbol, i.display_name, i.market, i.settle
                FROM alert_rules r JOIN instruments i ON i.id=r.instrument_id WHERE r.id=?
                """,
                (rule_id,),
            ).fetchone()
        return self._normalize_rule(row)

    def list_alert_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE r.enabled=1" if enabled_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.*, i.symbol, i.display_name, i.market, i.settle
                FROM alert_rules r JOIN instruments i ON i.id=r.instrument_id
                {where} ORDER BY r.created_at DESC
                """
            ).fetchall()
        return [self._normalize_rule(row) for row in rows if row is not None]

    def update_alert_rule(self, rule_id: int, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        allowed = {
            "name", "interval", "match_mode", "min_confidence", "confirmed_only",
            "lookback_bars", "coincidence_bars", "cooldown_seconds", "browser_notify",
            "sound", "enabled", "patterns_json", "last_triggered_at",
        }
        updates = []
        params: list[Any] = []
        for key, value in payload.items():
            if key == "patterns":
                key = "patterns_json"
                value = json.dumps(value or [], ensure_ascii=False)
            if key not in allowed:
                continue
            if key in {"confirmed_only", "browser_notify", "sound", "enabled"}:
                value = int(bool(value))
            updates.append(f"{key}=?")
            params.append(value)
        if updates:
            updates.append("updated_at=?")
            params.append(int(time.time()))
            params.append(rule_id)
            with self.connect() as conn:
                conn.execute(f"UPDATE alert_rules SET {', '.join(updates)} WHERE id=?", params)
        return self.get_alert_rule(rule_id)

    def delete_alert_rule(self, rule_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))

    def insert_alert_event(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO alert_events
                    (rule_id, instrument_id, interval, event_key, pattern_ids_json, payload_json, triggered_at, is_read)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        payload["rule_id"], payload["instrument_id"], payload["interval"], payload["event_key"],
                        json.dumps(payload["pattern_ids"], ensure_ascii=False),
                        json.dumps(payload["payload"], ensure_ascii=False), int(payload["triggered_at"]),
                    ),
                )
                event_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None
        return self.get_alert_event(event_id)

    def get_alert_event(self, event_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT e.*, r.name AS rule_name, i.symbol, i.display_name, i.market, i.settle
                FROM alert_events e
                JOIN alert_rules r ON r.id=e.rule_id
                JOIN instruments i ON i.id=e.instrument_id
                WHERE e.id=?
                """,
                (event_id,),
            ).fetchone()
        return self._normalize_event(row)

    def _normalize_event(self, row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        item = dict(row)
        item["pattern_ids"] = json.loads(item.pop("pattern_ids_json") or "[]")
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        item["is_read"] = bool(item["is_read"])
        return item

    def list_alert_events(self, since_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.*, r.name AS rule_name, i.symbol, i.display_name, i.market, i.settle
                FROM alert_events e
                JOIN alert_rules r ON r.id=e.rule_id
                JOIN instruments i ON i.id=e.instrument_id
                WHERE e.id>? ORDER BY e.id DESC LIMIT ?
                """,
                (since_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._normalize_event(row) for row in rows if row is not None]

    def mark_events_read(self, ids: Optional[Sequence[int]] = None) -> None:
        with self.connect() as conn:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"UPDATE alert_events SET is_read=1 WHERE id IN ({placeholders})", list(ids))
            else:
                conn.execute("UPDATE alert_events SET is_read=1")

    def unread_alert_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM alert_events WHERE is_read=0").fetchone()[0])

    def set_setting(self, key: str, value: Any) -> None:
        now = int(time.time())
        encoded = json.dumps(value, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, encoded, now),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def create_opportunity_run(self, settings: dict[str, Any], total_tasks: int) -> int:
        now = int(time.time())
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunity_runs
                (status, settings_json, total_tasks, completed_tasks, current_label, started_at)
                VALUES ('running', ?, ?, 0, '', ?)
                """,
                (json.dumps(settings, ensure_ascii=False), int(total_tasks), now),
            )
            return int(cur.lastrowid)

    def update_opportunity_run_progress(self, run_id: int, completed_tasks: int, current_label: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE opportunity_runs SET completed_tasks=?, current_label=? WHERE id=?",
                (int(completed_tasks), str(current_label), int(run_id)),
            )

    def finish_opportunity_run(self, run_id: int, status: str, error: Optional[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE opportunity_runs SET status=?, finished_at=?, error=? WHERE id=?",
                (str(status), int(time.time()), error, int(run_id)),
            )

    def get_opportunity_run(self, run_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM opportunity_runs WHERE id=?", (int(run_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json") or "{}")
        return item

    def latest_opportunity_run(self) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM opportunity_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json") or "{}")
        return item

    def list_opportunity_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM opportunity_runs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["settings"] = json.loads(item.pop("settings_json") or "{}")
            result.append(item)
        return result

    def replace_opportunity_task(
        self,
        run_id: int,
        task: dict[str, Any],
        results: Sequence[dict[str, Any]],
    ) -> list[int]:
        now = int(time.time())
        instrument_id = str(task["instrument_id"])
        interval = str(task["interval"])
        result_ids: list[int] = []
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM opportunity_results WHERE run_id=? AND instrument_id=? AND interval=?",
                (int(run_id), instrument_id, interval),
            )
            conn.execute(
                """
                INSERT INTO opportunity_tasks
                (run_id, instrument_id, interval, status, signals_count, actionable_count,
                 best_event_key, latest_price, candles_count, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, instrument_id, interval) DO UPDATE SET
                    status=excluded.status, signals_count=excluded.signals_count,
                    actionable_count=excluded.actionable_count, best_event_key=excluded.best_event_key,
                    latest_price=excluded.latest_price, candles_count=excluded.candles_count,
                    error=excluded.error, updated_at=excluded.updated_at
                """,
                (
                    int(run_id), instrument_id, interval, str(task.get("status") or "no_signal"),
                    int(task.get("signals_count") or 0), int(task.get("actionable_count") or 0),
                    task.get("best_event_key"), task.get("latest_price"),
                    int(task.get("candles_count") or 0), task.get("error"), now,
                ),
            )
            for row in results:
                payload = {
                    "event": row.get("event") or {},
                    "horizon_stats": row.get("horizon_stats") or [],
                    "duration_label": row.get("duration_label"),
                    "holding_label": row.get("holding_label"),
                }
                cur = conn.execute(
                    """
                    INSERT INTO opportunity_results
                    (run_id, instrument_id, interval, event_key, pattern, name, direction,
                     start_time, end_time, confidence, confirmed, age_bars, duration_bars,
                     duration_seconds, recommended_horizon, holding_seconds, samples, wins,
                     win_rate, ci_low, ci_high, avg_signed_return, avg_mfe, avg_mae,
                     history_quality, score, actionable, status, latest_price, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(run_id), instrument_id, interval, str(row["event_key"]),
                        str(row["pattern"]), str(row["name"]), str(row["direction"]),
                        int(row["start_time"]), int(row["end_time"]), float(row["confidence"]),
                        int(bool(row.get("confirmed"))), int(row.get("age_bars") or 0),
                        int(row.get("duration_bars") or 0), int(row.get("duration_seconds") or 0),
                        row.get("recommended_horizon"), row.get("holding_seconds"),
                        int(row.get("samples") or 0), int(row.get("wins") or 0),
                        row.get("win_rate"), row.get("ci_low"), row.get("ci_high"),
                        row.get("avg_signed_return"), row.get("avg_mfe"), row.get("avg_mae"),
                        float(row.get("history_quality") or 0), float(row.get("score") or 0),
                        int(bool(row.get("actionable"))), str(row.get("status") or "watch"),
                        row.get("latest_price"), json.dumps(payload, ensure_ascii=False), now,
                    ),
                )
                result_ids.append(int(cur.lastrowid))
        return result_ids

    def opportunity_results(self, run_id: Optional[int] = None) -> dict[str, Any]:
        run = self.get_opportunity_run(int(run_id)) if run_id else self.latest_opportunity_run()
        if run is None:
            return {"run": None, "tasks": [], "summary": {}}
        with self.connect() as conn:
            task_rows = conn.execute(
                """
                SELECT t.*, i.symbol, i.display_name, i.market, i.settle, i.is_stock
                FROM opportunity_tasks t JOIN instruments i ON i.id=t.instrument_id
                WHERE t.run_id=?
                ORDER BY t.actionable_count DESC, t.status, i.symbol, t.interval
                """,
                (int(run["id"]),),
            ).fetchall()
            result_rows = conn.execute(
                """
                SELECT r.*, i.symbol, i.display_name, i.market, i.settle, i.is_stock
                FROM opportunity_results r JOIN instruments i ON i.id=r.instrument_id
                WHERE r.run_id=?
                ORDER BY r.actionable DESC, r.score DESC, r.confidence DESC
                """,
                (int(run["id"]),),
            ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in result_rows:
            item = dict(row)
            payload = json.loads(item.pop("payload_json") or "{}")
            item.update(payload)
            item["confirmed"] = bool(item["confirmed"])
            item["actionable"] = bool(item["actionable"])
            item["is_stock"] = bool(item["is_stock"])
            grouped.setdefault((item["instrument_id"], item["interval"]), []).append(item)
        tasks = []
        for row in task_rows:
            item = dict(row)
            item["is_stock"] = bool(item["is_stock"])
            item["signals"] = grouped.get((item["instrument_id"], item["interval"]), [])
            item["best_signal"] = next((s for s in item["signals"] if s["event_key"] == item.get("best_event_key")), item["signals"][0] if item["signals"] else None)
            tasks.append(item)
        actionable = sum(int(row.get("actionable_count") or 0) for row in tasks)
        bullish = sum(1 for row in result_rows if row["actionable"] and row["direction"] == "bullish")
        bearish = sum(1 for row in result_rows if row["actionable"] and row["direction"] == "bearish")
        summary = {
            "tasks": len(tasks),
            "actionable": actionable,
            "bullish": bullish,
            "bearish": bearish,
            "watch": sum(1 for row in tasks if row["status"] == "watch"),
            "no_signal": sum(1 for row in tasks if row["status"] == "no_signal"),
            "errors": sum(1 for row in tasks if row["status"] == "error"),
        }
        return {"run": run, "tasks": tasks, "summary": summary}
