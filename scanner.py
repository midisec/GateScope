#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Background watchlist updater and chart-pattern alert scanner."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Optional

from database import Database
from gate_client import GateClient, GateError, INTERVAL_SECONDS
from pattern_engine import CATALOG_BY_ID, scan_patterns


class BackgroundScanner:
    def __init__(
        self,
        db: Database,
        gate: GateClient,
        update_seconds: int = 15,
        catalog_refresh_seconds: int = 1800,
        on_status: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.db = db
        self.gate = gate
        self.update_seconds = max(5, int(update_seconds))
        self.catalog_refresh_seconds = max(300, int(catalog_refresh_seconds))
        self.on_status = on_status
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_catalog = 0
        self._last_watch_update: dict[tuple[int, str], float] = defaultdict(float)
        self.status: dict[str, Any] = {
            "running": False,
            "last_cycle": None,
            "last_error": None,
            "updated_instruments": 0,
            "triggered_alerts": 0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gate-pattern-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def _emit(self) -> None:
        if self.on_status:
            try:
                self.on_status(dict(self.status))
            except Exception:
                pass

    def refresh_catalog(self) -> int:
        rows = self.gate.fetch_catalog(include_btc_futures=True)
        count = self.db.upsert_instruments(rows)
        self._last_catalog = int(time.time())
        return count

    def _run(self) -> None:
        self.status["running"] = True
        self._emit()
        while not self._stop.is_set():
            started = time.time()
            updated = 0
            triggered = 0
            try:
                if self.db.instrument_count() == 0 or started - self._last_catalog >= self.catalog_refresh_seconds:
                    self.refresh_catalog()

                watchlist = [w for w in self.db.list_watchlist() if w["enabled"]]
                rules = self.db.list_alert_rules(enabled_only=True)
                rules_by_instrument: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for rule in rules:
                    rules_by_instrument[rule["instrument_id"]].append(rule)

                requested: dict[str, set[str]] = defaultdict(set)
                refresh_for: dict[tuple[str, str], int] = {}
                for item in watchlist:
                    requested[item["instrument_id"]].add(item["default_interval"])
                    refresh_for[(item["instrument_id"], item["default_interval"])] = item["refresh_seconds"]
                for rule in rules:
                    requested[rule["instrument_id"]].add(rule["interval"])
                    refresh_for.setdefault((rule["instrument_id"], rule["interval"]), self.update_seconds)

                for instrument_id, intervals in requested.items():
                    instrument = self.db.get_instrument(instrument_id)
                    if not instrument:
                        continue
                    for interval in intervals:
                        key = (hash(instrument_id), interval)
                        refresh_seconds = max(5, int(refresh_for.get((instrument_id, interval), self.update_seconds)))
                        if started - self._last_watch_update[key] < refresh_seconds:
                            continue
                        _, latest = self.db.candle_bounds(instrument_id, interval)
                        rows = self.gate.fetch_latest(instrument, interval, latest)
                        self.db.upsert_candles(instrument_id, interval, rows)
                        self._last_watch_update[key] = started
                        updated += 1

                    for rule in rules_by_instrument.get(instrument_id, []):
                        event = self._evaluate_rule(rule)
                        if event:
                            triggered += 1

                self.status.update(
                    {
                        "last_cycle": int(time.time()),
                        "last_error": None,
                        "updated_instruments": updated,
                        "triggered_alerts": triggered,
                    }
                )
            except Exception as exc:
                self.status["last_error"] = str(exc)
                self.status["last_cycle"] = int(time.time())
            self._emit()
            elapsed = time.time() - started
            wait = max(1.0, self.update_seconds - elapsed)
            self._wake.wait(wait)
            self._wake.clear()
        self.status["running"] = False
        self._emit()

    def _evaluate_rule(self, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
        now = int(time.time())
        last_triggered = int(rule.get("last_triggered_at") or 0)
        if now - last_triggered < int(rule["cooldown_seconds"]):
            return None
        candles = self.db.latest_candles(
            rule["instrument_id"], rule["interval"], int(rule["lookback_bars"])
        )
        if len(candles) < 30:
            return None
        result = scan_patterns(
            candles,
            selected_patterns=rule["patterns"],
            min_confidence=float(rule["min_confidence"]),
            confirmed_only=bool(rule["confirmed_only"]),
            max_bars=int(rule["lookback_bars"]),
            max_events_per_pattern=5,
        )
        events = result["events"]
        if not events:
            return None
        latest_ts = int(candles[-1]["ts"])
        interval_seconds = INTERVAL_SECONDS[rule["interval"]]
        recent_cutoff = latest_ts - max(1, int(rule["coincidence_bars"])) * interval_seconds
        recent = [e for e in events if int(e["end_time"]) >= recent_cutoff]
        if not recent:
            return None

        selected = set(rule["patterns"])
        if rule["match_mode"] == "all":
            latest_by_pattern: dict[str, dict[str, Any]] = {}
            for event in recent:
                current = latest_by_pattern.get(event["pattern"])
                if current is None or event["end_time"] > current["end_time"] or event["confidence"] > current["confidence"]:
                    latest_by_pattern[event["pattern"]] = event
            if not selected.issubset(latest_by_pattern):
                return None
            matched = [latest_by_pattern[p] for p in sorted(selected)]
        else:
            matched = sorted(recent, key=lambda e: (e["end_time"], e["confidence"]), reverse=True)
            matched = [matched[0]]

        pattern_ids = sorted({e["pattern"] for e in matched})
        max_end = max(int(e["end_time"]) for e in matched)
        key_material = f"{rule['id']}:{max_end}:{','.join(pattern_ids)}"
        event_key = hashlib.sha1(key_material.encode("utf-8")).hexdigest()
        payload = {
            "rule_id": rule["id"],
            "instrument_id": rule["instrument_id"],
            "interval": rule["interval"],
            "event_key": event_key,
            "pattern_ids": pattern_ids,
            "payload": {
                "match_mode": rule["match_mode"],
                "patterns": matched,
                "latest_ts": latest_ts,
                "message": self._message(rule, matched),
                "browser_notify": bool(rule["browser_notify"]),
                "sound": bool(rule["sound"]),
            },
            "triggered_at": now,
        }
        inserted = self.db.insert_alert_event(payload)
        if inserted:
            self.db.update_alert_rule(rule["id"], {"last_triggered_at": now})
        return inserted

    @staticmethod
    def _message(rule: dict[str, Any], events: list[dict[str, Any]]) -> str:
        names = "、".join(event["name"] for event in events)
        confidences = ", ".join(f"{event['confidence'] * 100:.0f}%" for event in events)
        logic = "同时满足" if rule["match_mode"] == "all" else "检测到"
        return f"{rule['symbol']} {rule['interval']} {logic}：{names}（置信度 {confidences}）"
