"""Incremental public-data cache: latest first, bounded history, durable retries."""
from datetime import datetime, timezone
from typing import Any, Callable

from gate_client import GateRateLimited
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.adapters.gate_history import ArchiveFormatError, ArchiveUnavailable, MAX_ARCHIVE_PAGE_BARS, month_bounds


class MarketSyncRuntime:
    def __init__(self, repository, client, *, archive=None, warmer=None, now: Callable[[], datetime] | None = None):
        self.repository, self.client = repository, client
        self.archive = archive
        self.warmer = warmer
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run_cycle(self, *, progress=lambda status: None, stopped=lambda: False, live=True, history=True) -> dict:
        repo, client = self.repository, self.client
        subscriptions = repo.subscriptions()
        states = repo.sync_states(subscriptions)
        result: dict[str, Any] = {"subscription_count": len(subscriptions), "updated_subscriptions": 0,
            "inserted_bars": 0, "history_pages": 0, "degraded": False, "safe_error_code": None}
        def key(s):
            return (s.instrument_id, s.timeframe)
        due = sorted(subscriptions, key=lambda s: states[key(s)]["next_live_at"])
        retry_at = repo.provider_retry_at()
        if retry_at and retry_at > self.now():
            return {**result, "rate_limited": True, "safe_error_code": "MARKET_RATE_LIMITED", "degraded": True}
        # One cycle is bounded. Oldest due subscriptions take precedence over list order.
        warmed=[]
        for s in ([s for s in due if states[key(s)]["next_live_at"] <= self.now()][:16] if live else []):
            if stopped():
                return result
            progress(result)
            retry_at=repo.provider_retry_at()
            if retry_at and retry_at>self.now():break
            try:
                step = TIMEFRAME_SECONDS[s.timeframe]
                end = int(self.now().timestamp())
                start = end // step * step - 3 * step
                rows, _ = client.fetch_candles(s.gate_instrument(), s.timeframe, start, end)
                observed = self.now()
                if not rows or max(r[0] for r in rows) < start + 2 * step:
                    raise ValueError("empty or stale current candles")
                result["inserted_bars"] += repo.upsert_final_bars(s, rows, observed_at=observed)
                repo.upsert_live_bar(s, rows, observed_at=observed)
                repo.mark_live(s, observed)
                result["updated_subscriptions"] += 1
                warmed.append(s)
            except GateRateLimited as exc:
                repo.pause_provider(self.now(), exc.retry_after)
                return {**result, "degraded": True, "rate_limited": True, "safe_error_code": "MARKET_RATE_LIMITED"}
            except Exception:
                repo.mark_live(s, self.now(), error="MARKET_POLL_FAILED",
                    delay=min(300, 5 * 2 ** min(states[key(s)]["live_failures"], 6)))
                result.update(degraded=True, safe_error_code="MARKET_SUBSCRIPTION_UPDATE_FAILED")
        for s in warmed:
            if stopped():return result
            if self.warmer:
                try:self.warmer.warm(s)
                except Exception:result.update(snapshot_error="CHART_SNAPSHOT_WARM_FAILED")
        history_due = sorted(subscriptions, key=lambda s: states[key(s)]["next_history_at"])
        # Never let one missing instrument starve every other pair or hold a cycle indefinitely.
        for s in ([s for s in history_due if states[key(s)]["next_history_at"] <= self.now()][:2] if history else []):
            if stopped():
                return result
            progress(result)
            retry_at=repo.provider_retry_at()
            if retry_at and retry_at>self.now():break
            plan = repo.history_plan(s, self.now())
            if not plan["gap"]:
                repo.mark_history(s, plan, self.now(), state="SOURCE_LIMIT" if plan.get("missing_bars",0) else "READY", delay=900)
                continue
            try:
                start, end = plan["gap"]
                if self.archive is not None:self.archive.check_capacity()
                step=TIMEFRAME_SECONDS[s.timeframe]
                recent_from=(int(self.now().timestamp())//step-9398)*step
                archived=self.archive is not None and end<recent_from
                if self.archive is not None and not archived:start=max(start,recent_from)
                source=None
                if archived:
                    # Fill/prove one bounded page even when the newest gap is
                    # just a single missing bar inside a sparse official file.
                    # Repeated archive rows never replace existing REST bars.
                    if plan.get("target_from") is not None:
                        start=max(int(plan["target_from"].timestamp()),end-(MAX_ARCHIVE_PAGE_BARS-1)*step)
                    rows,source=self.archive.fetch(s.gate_instrument(),s.timeframe,start,end)
                    if not rows:
                        month_start,month_end=month_bounds(end)
                        queried_from=max(start,month_start,source.get("queried_from",month_start))
                        queried_to=min(end,month_end-step,source.get("queried_to",end))
                        # Never defer the preceding month: fetch selected only
                        # the end-month file, including for legacy mock sources.
                        raise ArchiveUnavailable(queried_from,queried_to,"ARCHIVE_RANGE_EMPTY")
                else:
                    rows, _ = client.fetch_candles(s.gate_instrument(), s.timeframe, start, end)
                    if not rows and self.archive is not None:raise ArchiveUnavailable(start,end,"REST_RANGE_EMPTY",300)
                rows = [r for r in rows if start <= r[0] <= end]
                observed = self.now()
                inserted = repo.upsert_final_bars(s, rows, observed_at=observed, **({"archive_source":source} if source else {}))
                if archived and source and source.get("missing_ranges"):
                    repo.defer_history_ranges(s,source["missing_ranges"],observed,
                        reason="ARCHIVE_RANGE_EMPTY",delay=21600)
                result["inserted_bars"] += inserted
                result["history_pages"] += 1
                updated = repo.history_plan(s, observed)
                if not updated["gap"]:
                    state, delay = ("SOURCE_LIMIT" if updated.get("missing_bars",0) else "READY"), 900
                elif not rows:
                    state, delay = ("GAP", 300) if plan["internal_gap"] else ("SOURCE_LIMIT", 21600)
                else:
                    state, delay = "BACKFILLING", 1
                repo.mark_history(s, updated, observed, state=state, delay=delay)
                if self.warmer:self.warmer.warm(s)
            except ArchiveFormatError as exc:
                repo.mark_history(s,plan,self.now(),state="RETRYING",delay=exc.retry_after,error=exc.reason)
                result.update(degraded=True,safe_error_code=exc.reason)
            except ArchiveUnavailable as exc:
                observed=self.now()
                repo.defer_history(s,exc.start,exc.end,observed,reason=exc.reason,delay=exc.retry_after)
                updated=repo.history_plan(s,observed)
                repo.mark_history(s,updated,observed,state="BACKFILLING" if updated["gap"] else "SOURCE_LIMIT",
                    delay=1 if updated["gap"] else min(exc.retry_after,900),error=exc.reason)
            except GateRateLimited as exc:
                repo.mark_history(s, plan, self.now(), state="RETRYING", delay=exc.retry_after, error="MARKET_RATE_LIMITED")
                repo.pause_provider(self.now(), exc.retry_after)
                return {**result, "degraded": True, "rate_limited": True, "safe_error_code": "MARKET_RATE_LIMITED"}
            except Exception as exc:
                safe="HISTORY_DISK_LOW" if str(exc)=="HISTORY_DISK_LOW" else "MARKET_HISTORY_FAILED"
                repo.mark_history(s, plan, self.now(), state="RETRYING", delay=60, error=safe)
        progress(result)
        return result
