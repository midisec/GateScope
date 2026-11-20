"""Append-only lifecycle decisions and prequential completion calibration.

Not seeing a proposal again is NOT failure. Frozen price rules or a fixed
deadline resolve it; missing observations censor it. Failure is never deleted.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Callable

from gatescope.quant.pattern_study import WINDOW, catalog, digest, summarize as return_summary
from gatescope.quant.structure_detection import VERSION, bucket, known_points, proposals, value_at
from gatescope.quant.structure_forecast import issue, assess, learn_formed_leg
from gatescope.quant.stage_environment import stage_environment_key

TERMINAL = {"CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED"}
PHASE_LABELS = {
    "FORMING": "形成中",
    "FORMED": "已形成",
    "CONFIRMED": "已确认",
    "INVALIDATED": "已失效",
    "EXPIRED": "到期未完成",
    "CENSORED": "观测中断",
}
REASONS = {
    "ADMITTED": "首次识别，固定初始条件",
    "PROGRESS": "新增已确认枢轴",
    "FORMED": "结构形成，开始检验确认条件",
    "BREAKOUT": "连续两根收盘越过含 ATR 缓冲的确认线",
    "INVALIDATION": "触及预设结构失效条件",
    "DEADLINE": "观察期限内未完成确认",
    "APEX": "收敛边界相交后仍未确认",
    "DATA_GAP": "行情观测不连续，不推断成败",
    "MODEL_CHANGED": "模型规则变更，旧候选停止评估，不计成败",
    "FORECAST_OBSERVED": "首次预测目标的后续观测已记录，触价不等于形态确认",
    "MONITORING_STOPPED": "用户取消监控，保留最后已知观测，不推断成败",
}
MAX_RECORDS = 16000
MAX_ACTIVE = 512
TRIALS = len(catalog()) * 2 * 5 * 2  # Both formation and breakout-confirmation endpoints.


def cell_key(pattern: str, phase: str, score_bucket: str) -> str:
    return f"{pattern}:{phase}:{score_bucket}"


def probability(wins: int, count: int) -> dict:
    # Avoid allocating N synthetic trades on every live refresh.
    import math
    from statistics import NormalDist

    result = {
        "sample_count": count,
        "successes": wins,
        "rate": wins / count if count else None,
        "probability": wins / count if count >= 20 else None,
        "interval": None,
        "minimum_sample": 20,
        "method": f"Wilson / Bonferroni {TRIALS}",
        "confidence_level": 0.95,
        "precision": "INSUFFICIENT",
        "hypotheses": TRIALS,
    }
    if count >= 20:
        p, z = wins / count, NormalDist().inv_cdf(1 - 0.05 / (2 * TRIALS))
        scale = 1 + z * z / count
        center = (p + z * z / (2 * count)) / scale
        half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / scale
        result["interval"] = {"lower": max(0, center - half), "upper": min(1, center + half)}
        result["precision"] = "NARROW" if 2 * half <= 0.2 else "MODERATE" if 2 * half <= 0.4 else "WIDE"
    return result


def seed_prediction(state: dict, event: dict, phase: str) -> dict:
    cell = state.get("training", {}).get(cell_key(event["pattern"], phase, bucket(event)), [0, 0])
    result = probability(cell[1], cell[0])
    formation = state.get("formation_training", {}).get(
        cell_key(event["pattern"], phase, bucket(event)), [0, 0]
    )
    return {
        **result,
        "formation": probability(formation[1], formation[0]) if phase == "FORMING" else None,
        "score_bucket": bucket(event),
        "basis": "PRIOR_RESOLVED_ONLY",
    }


def sample_stage(
    state: dict,
    record: dict,
    phase: str,
    index: int,
    ts: int,
    prediction_provider=None,
    *,
    environment_key=None,
) -> None:
    key = f"{record['pattern']}:{phase}"
    previous = state.setdefault("sampling", {}).get(key)
    other = state["records"].get(previous) if previous else None
    sampled = not other or (other.get("resolved_index") is not None and other["resolved_index"] < index)
    if sampled:
        state["sampling"][key] = record["id"]
    prediction = seed_prediction(state, record["latest"], phase)
    if prediction_provider:
        reference = prediction_provider({**record["latest"], "phase": phase}, ts)
        prediction = {
            **reference,
            "probability": reference["rate"] if reference["sample_count"] >= 20 else None,
        }
    record["stages"][phase] = {
        "index": index,
        "time": ts,
        "sampled": sampled,
        "score_bucket": bucket(record["latest"]),
        "prediction": prediction,
    }
    if environment_key is not None:
        record["stages"][phase]["environment_key"] = environment_key


def append_transition(
    record: dict, phase: str, index: int, ts: int, reason: str, journal: list, snapshot_at: str
) -> None:
    previous = record.get("phase")
    record["phase"], record["last_index"], record["observed_time"] = phase, index, ts
    record["reason"], record["sequence"] = reason, record.get("sequence", 0) + 1
    record["snapshot_at"] = snapshot_at
    record["latest"] = {
        **record["latest"],
        "phase": phase,
        "confirmed": phase == "CONFIRMED",
        "stage": REASONS[reason],
        "id": record["id"],
        "observed_time": ts,
        "deadline_time": record["deadline_time"],
    }
    record["latest"]["evidence_id"] = digest(
        [record["id"], record["sequence"], record["latest"], ts, snapshot_at]
    )
    if phase in TERMINAL:
        record["resolved_index"], record["resolved_time"] = index, ts
    journal.append(
        {
            "candidate_id": record["id"],
            "sequence": record["sequence"],
            "from_phase": previous,
            "phase": phase,
            "observed_time": ts,
            "record": copy.deepcopy(record),
        }
    )


def resolve_rule(record: dict, bar: dict, index: int) -> tuple[str, str] | None:
    levels = record["latest"]["levels"]
    direction = record["latest"]["signal_direction"]
    # Additional fixed-path constraints can have two sides (a complete cycle)
    # or a strict upper bound (wave 3 may tie, but must not be the shortest).
    # These are wick-based hard rules, independent of the legacy close/ATR rule.
    for limit in levels.get("hard_limits", []):
        side = limit["side"]
        price = bar["high"] if side > 0 else bar["low"]
        distance = side * (price - limit["price"])
        invalid = distance >= 0 if limit.get("inclusive", True) else distance > 0
        if invalid:
            return "INVALIDATED", "INVALIDATION"
    if levels.get("invalid_side"):
        side = levels["invalid_side"]
        limit = (
            value_at(levels["invalid_edge"], bar["ts"])
            if "invalid_edge" in levels
            else levels["invalid_price"]
        )
        price = (
            bar["high"]
            if levels.get("hard") and side > 0
            else bar["low"]
            if levels.get("hard")
            else bar["close"]
        )
        invalid = side * (price - limit) >= (0 if levels.get("hard") else levels["buffer"])
        if invalid:
            return "INVALIDATED", "INVALIDATION"
    if "upper" in levels and value_at(levels["upper"], bar["ts"]) <= value_at(levels["lower"], bar["ts"]):
        return "EXPIRED", "APEX"
    if record["phase"] == "FORMED" and index > record["stages"]["FORMED"]["index"]:
        side = 1 if direction == "bullish" else -1 if direction == "bearish" else 0
        if "trigger" in levels:
            passed = (
                side
                if side * (bar["close"] - value_at(levels["trigger"], bar["ts"])) > levels["buffer"]
                else 0
            )
        elif "upper" in levels:
            up = bar["close"] > value_at(levels["upper"], bar["ts"]) + levels["buffer"]
            down = bar["close"] < value_at(levels["lower"], bar["ts"]) - levels["buffer"]
            passed = 1 if up and side >= 0 else -1 if down and side <= 0 else 0
        else:
            passed = 0
        record["confirm_streak"] = (
            record.get("confirm_streak", 0) + 1
            if passed and record.get("confirm_side") == passed
            else int(bool(passed))
        )
        record["confirm_side"] = passed
        if record["confirm_streak"] >= levels["closes"]:
            record["latest"]["signal_direction"] = "bullish" if passed > 0 else "bearish"
            return "CONFIRMED", "BREAKOUT"
    if index >= record["deadline_index"]:
        return "EXPIRED", "DEADLINE"
    return None


def observe(
    candles: list[dict],
    index: int,
    state: dict,
    *,
    detector: Callable = proposals,
    snapshot_at: str = "",
    source_mode: str = "REPLAY",
    index_offset: int = 0,
    prediction_provider=None,
    terminal_guard=None,
) -> list[dict]:
    """Each call commits at most one closed bar; immutable journal returned to persistence."""
    absolute = index + index_offset
    prefix = candles[max(0, index + 1 - WINDOW) : index + 1]
    bar = prefix[-1]
    step = state.get("step_seconds") or (prefix[-1]["ts"] - prefix[-2]["ts"] if len(prefix) > 1 else 1)
    # Never detect across a missing candle; the expected timeframe stays fixed.
    for pos in range(len(prefix) - 1, 0, -1):
        if prefix[pos]["ts"] - prefix[pos - 1]["ts"] != step:
            prefix = prefix[pos:]
            break
    ts = bar["ts"] + step
    environment_ready, environment = False, None

    def current_environment():
        nonlocal environment_ready, environment
        if not environment_ready:
            environment = stage_environment_key(prefix, step)
            environment_ready = True
        return environment

    records = state.setdefault("records", {})
    state.setdefault("facts", [])
    journal, resolved = [], []
    # Delayed by one bar: a new prediction cannot train on an outcome from its own bar.
    for key, success in state.pop("pending_training", []):
        cell = state.setdefault("training", {}).setdefault(key, [0, 0])
        cell[0] += 1
        cell[1] += success
    for key, success in state.pop("pending_formation_training", []):
        cell = state.setdefault("formation_training", {}).setdefault(key, [0, 0])
        cell[0] += 1
        cell[1] += success
    for key, sample in state.pop("pending_forecast_training", []):
        cell = state.setdefault("forecast_training", {}).setdefault(key, [])
        cell.append(sample)
        state["forecast_training"][key] = cell[-256:]
    last_ts = state.get("last_ts")
    if last_ts is not None and bar["ts"] <= last_ts:
        raise ValueError("STRUCTURE_NON_MONOTONE")
    gap = last_ts is not None and bar["ts"] - last_ts != state["step_seconds"]
    active = state.setdefault("active", [])

    def settle(record, decision):
        # Assess once, against this bar's final decision. Assessing a touch
        # before a newly known hard boundary would incorrectly freeze TOUCHED
        # instead of AMBIGUOUS when both happen within the same OHLC bar.
        assess(record, bar, ts, decision)
        phase, reason = decision
        append_transition(record, phase, absolute, ts, reason, journal, snapshot_at)
        active.remove(record["id"])
        resolved.append(record)
        if phase == "CONFIRMED":
            side = 1 if record["latest"]["signal_direction"] == "bullish" else -1
            state["facts"].append(
                [record["pattern"], "CONFIRMED", absolute, side, record["latest"]["evidence_id"]]
            )

    for identifier in list(active):
        r = records[identifier]
        decision = ("CENSORED", "DATA_GAP") if gap else resolve_rule(r, bar, absolute)
        if decision:
            settle(r, decision)
    if detector is proposals:
        state["detection_diagnostics"] = {}
        detected = detector(prefix, diagnostics=state["detection_diagnostics"])
    else:
        detected = detector(prefix)
    blocked = (
        set(terminal_guard([e["id"] for e in detected if e["id"] not in records]))
        if terminal_guard
        else set()
    )
    for e in detected:
        identifier = e["id"]
        if identifier in blocked:
            continue
        if identifier in records:
            r = records[identifier]
            if r["phase"] in TERMINAL:
                continue  # No resurrection of a failed candidate under the same anchors.
            if r["phase"] == "FORMING" and (
                e["phase"] == "FORMED" or len(known_points(e)) > len(known_points(r["latest"]))
            ):
                # Initial evidence stays frozen. Only an explicit new snapshot progresses it.
                learn_formed_leg(r, e, state, ts)
                if e["phase"] == "FORMED":
                    r["pre_formation_forecast"] = copy.deepcopy(r["latest"].get("forecast"))
                    point = known_points(e)[-1]
                    r["structure_formed_at"] = {
                        "price": point["price"],
                        "pivot_time": point["time"],
                        "known_at": ts,
                    }
                r["latest"] = copy.deepcopy(e)
                issue(r, state, ts, bar["close"])
                r["latest"]["formation_observation"] = r.get("formation_observation")
                r["latest"]["pre_formation_forecast"] = r.get("pre_formation_forecast")
                r["latest"]["structure_formed_at"] = r.get("structure_formed_at")
                if e["phase"] == "FORMED":
                    sample_stage(
                        state,
                        r,
                        "FORMED",
                        absolute,
                        ts,
                        prediction_provider,
                        environment_key=current_environment(),
                    )
                    append_transition(r, "FORMED", absolute, ts, "FORMED", journal, snapshot_at)
                    side = {"bullish": 1, "bearish": -1}.get(e["signal_direction"], 0)
                    state["facts"].append(
                        [r["pattern"], "FORMED", absolute, side, r["latest"]["evidence_id"]]
                    )
                else:
                    append_transition(r, "FORMING", absolute, ts, "PROGRESS", journal, snapshot_at)
                # New geometry may introduce a tighter invalidation boundary.
                # Keep its observed snapshot, then record failure on THIS bar;
                # recovery on the next bar must not erase an observed breach.
                decision = resolve_rule(r, bar, absolute)
                if decision:
                    settle(r, decision)
            continue
        if len(active) >= MAX_ACTIVE or len(records) >= MAX_RECORDS:
            raise ValueError("PATTERN_STUDY_TOO_DENSE")
        deadline = absolute + e["levels"]["expiry_bars"]
        r = {
            "id": identifier,
            "pattern": e["pattern"],
            "first": copy.deepcopy(e),
            "latest": copy.deepcopy(e),
            "first_index": absolute,
            "first_observed_time": ts,
            "first_snapshot_at": snapshot_at,
            "source_mode": source_mode,
            "deadline_index": deadline,
            "deadline_time": ts + e["levels"]["expiry_bars"] * step,
            "stages": {},
        }
        records[identifier] = r
        sample_stage(
            state, r, e["phase"], absolute, ts, prediction_provider, environment_key=current_environment()
        )
        issue(r, state, ts, bar["close"])
        append_transition(r, e["phase"], absolute, ts, "ADMITTED", journal, snapshot_at)
        active.append(identifier)
        if e["phase"] == "FORMED":
            side = {"bullish": 1, "bearish": -1}.get(e["signal_direction"], 0)
            state["facts"].append([e["pattern"], "FORMED", absolute, side, r["latest"]["evidence_id"]])
        decision = resolve_rule(r, bar, absolute)
        if decision:
            # Initial forecasts are issued at this bar's close. assess() must
            # not backfill an earlier intrabar touch into that new forecast.
            settle(r, decision)
    for identifier in active:
        r = records[identifier]
        if assess(r, bar, ts):
            append_transition(r, r["phase"], absolute, ts, "FORECAST_OBSERVED", journal, snapshot_at)
    for r in resolved:
        if r["phase"] == "CENSORED":
            continue
        for phase, stage in r["stages"].items():
            if stage["sampled"]:
                state.setdefault("pending_training", []).append(
                    [cell_key(r["pattern"], phase, stage["score_bucket"]), int(r["phase"] == "CONFIRMED")]
                )
                if phase == "FORMING":
                    state.setdefault("pending_formation_training", []).append(
                        [cell_key(r["pattern"], phase, stage["score_bucket"]), int("FORMED" in r["stages"])]
                    )
    state["last_ts"], state["step_seconds"], state["last_index"] = bar["ts"], step, absolute
    return journal


def completion_summary(state: dict, *, through: int | None = None) -> tuple[list[dict], list[list]]:
    grouped = defaultdict(list)
    training = []
    for r in state.get("records", {}).values():
        for phase, stage in r["stages"].items():
            grouped[(r["pattern"], phase, stage["score_bucket"])].append((r, stage))
            if stage["sampled"] and r["phase"] in TERMINAL - {"CENSORED"}:
                training.append(
                    [
                        r["pattern"],
                        phase,
                        stage["score_bucket"],
                        stage["time"],
                        r["resolved_time"],
                        int(r["phase"] == "CONFIRMED"),
                        r["id"],
                        int("FORMED" in r["stages"]),
                    ]
                )
    rows = []
    for meta in catalog():
        for phase in ("FORMING", "FORMED"):
            for score in ("ALL", "UNSCORED", "LOW", "MID", "HIGH"):
                cases = [
                    case
                    for (p, s, b), items in grouped.items()
                    if p == meta["id"] and s == phase and (score == "ALL" or b == score)
                    for case in items
                ]
                counts = {p: sum(r["phase"] == p for r, _ in cases) for p in PHASE_LABELS}
                completed = [
                    (r, s) for r, s in cases if s["sampled"] and r["phase"] in TERMINAL - {"CENSORED"}
                ]
                wins = sum(r["phase"] == "CONFIRMED" for r, _ in completed)
                scored = [
                    (s["prediction"]["probability"], int(r["phase"] == "CONFIRMED"))
                    for r, s in completed
                    if s["prediction"]["probability"] is not None
                ]
                rows.append(
                    {
                        "pattern": meta["id"],
                        "phase": phase,
                        "score_bucket": score,
                        "total": len(cases),
                        **counts,
                        "pending": counts["FORMING"] + counts["FORMED"],
                        "overlap_excluded": sum(not s["sampled"] for _, s in cases),
                        "formed_count": sum("FORMED" in r["stages"] for r, _ in cases),
                        **probability(wins, len(completed)),
                        "formation": probability(
                            sum("FORMED" in r["stages"] for r, _ in completed), len(completed)
                        )
                        if phase == "FORMING"
                        else None,
                        "calibration_count": len(scored),
                        "brier": sum((p - y) ** 2 for p, y in scored) / len(scored) if scored else None,
                    }
                )
    return rows, training


def summarize(candles: list[dict], state: dict, metadata: dict) -> dict:
    report = return_summary(candles, state, metadata)
    rows, training = completion_summary(state)
    return {
        **report,
        "version": VERSION,
        "completion_rows": rows,
        "training_samples": training,
        "candidate_count": len(state.get("records", {})),
        "lifecycle": True,
    }


def historical_reference(event: dict, report: dict | None, as_of: int) -> dict:
    phase = event["phase"]
    if phase not in {"FORMING", "FORMED"} or not report or report.get("version") != VERSION:
        return {**probability(0, 0), "basis": "NO_MATCHING_HISTORY"}
    score = bucket(event)
    rows = [
        r
        for r in report.get("training_samples", [])
        if r[0] == event["pattern"]
        and r[1] == phase
        and r[2] == score
        and r[4] < as_of
        and r[6] != event["id"]
    ]
    return {
        **probability(sum(r[5] for r in rows), len(rows)),
        "as_of": as_of,
        "score_bucket": score,
        "formation": probability(sum(r[7] for r in rows if len(r) > 7), sum(len(r) > 7 for r in rows))
        if phase == "FORMING"
        else None,
        "basis": "CURRENT_HISTORICAL_REFERENCE",
        "report_as_of": report["to_time"],
        "partial": report.get("partial", False),
    }
