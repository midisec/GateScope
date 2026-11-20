"""One delivery deadline shared by detection and queued signal delivery."""

from datetime import datetime, timedelta

from gatescope.domain.market import TIMEFRAME_SECONDS, utc

POLICY = "structure-alert-1"
LIVE_FRESHNESS_SECONDS = 45
MAX_SIGNAL_AGE_SECONDS = 900


def signal_lifetime(timeframe: str) -> timedelta:
    return timedelta(seconds=min(2 * TIMEFRAME_SECONDS[timeframe], MAX_SIGNAL_AGE_SECONDS))


def latest_closed_time(timeframe: str, now: datetime) -> datetime:
    stamp = utc(now)
    step = TIMEFRAME_SECONDS[timeframe]
    return datetime.fromtimestamp(int(stamp.timestamp()) // step * step, stamp.tzinfo)
