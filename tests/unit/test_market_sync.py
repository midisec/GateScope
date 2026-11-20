from datetime import datetime, timedelta, timezone
import gzip
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gate_client import GateClient, GateRateLimited
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.adapters.gate_history import ArchiveFormatError, GateHistoryArchive
from gatescope.application.market_sync import MarketSyncRuntime
from gatescope.domain.history import eligible_gap

NOW = datetime(2026, 9, 9, 12, 0, 20, tzinfo=timezone.utc)


class Repository:
    def __init__(self, count=2):
        self.pairs = [MarketSubscription(uuid4(), "GATE", "FUTURES_USDT", f"PAIR{i}_USDT", "USDT", "1m") for i in range(count)]
        self.states = {(s.instrument_id, s.timeframe): dict(next_live_at=NOW, next_history_at=NOW,
            live_failures=0, live_error_code=None) for s in self.pairs}
        self.events = []
        self.internal = False

    def subscriptions(self): return self.pairs
    def sync_states(self, subscriptions): return self.states
    def mark_live(self, s, now, *, error=None, delay=5):
        self.events.append(("live", s.symbol, error))
        self.states[(s.instrument_id, s.timeframe)].update(next_live_at=now + timedelta(seconds=delay), live_error_code=error)
    def pause_provider(self, now, delay):
        self.events.append(("pause", delay))
        for state in self.states.values():
            state.update(next_live_at=now + timedelta(seconds=delay), next_history_at=now + timedelta(seconds=delay), live_error_code="MARKET_RATE_LIMITED")
    def provider_retry_at(self):
        return max((s["next_live_at"] for s in self.states.values() if s["live_error_code"] == "MARKET_RATE_LIMITED"), default=None)
    def upsert_final_bars(self, s, rows, **kwargs): return len(rows)
    def upsert_live_bar(self, s, rows, **kwargs): self.events.append(("forming", s.symbol))
    def history_plan(self, s, now):
        return {"gap": (int(NOW.timestamp()) - 10000, int(NOW.timestamp()) - 9000), "internal_gap": self.internal}
    def mark_history(self, s, plan, now, **kwargs):
        self.events.append(("history", kwargs))
        self.states[(s.instrument_id,s.timeframe)]["next_history_at"]=now+timedelta(seconds=kwargs["delay"])


class Client:
    def __init__(self):
        self.calls = []
        self.fail = None
    def fetch_candles(self, instrument, timeframe, start, end):
        self.calls.append((instrument["symbol"], start, end))
        if self.fail and instrument["symbol"] == "PAIR0_USDT":
            raise self.fail
        if end < NOW.timestamp() - 1000:
            return [], False
        return [(int(NOW.timestamp()) // 60 * 60, 100, 102, 99, 101, 3)], False


def test_latest_precedes_bounded_history_and_one_failure_does_not_block_other_pair():
    repo, client = Repository(), Client()
    client.fail = RuntimeError("network")
    result = MarketSyncRuntime(repo, client, now=lambda: NOW).run_cycle()
    assert result["updated_subscriptions"] == 1
    assert client.calls[0][0] == "PAIR0_USDT" and client.calls[1][0] == "PAIR1_USDT"
    assert len(client.calls) == 4


def test_rate_limit_honors_retry_after_across_runtime_restart():
    repo, client = Repository(), Client()
    client.fail = GateRateLimited(321)
    result = MarketSyncRuntime(repo, client, now=lambda: NOW).run_cycle()
    assert result["rate_limited"] and len(client.calls) == 1
    assert ("pause", 321) in repo.events
    MarketSyncRuntime(repo, client, now=lambda: NOW + timedelta(seconds=30)).run_cycle()
    assert len(client.calls) == 1
    added = MarketSubscription(uuid4(), "GATE", "FUTURES_USDT", "NEW_USDT", "USDT", "1m")
    repo.pairs.append(added)
    repo.states[(added.instrument_id, added.timeframe)] = dict(next_live_at=NOW, next_history_at=NOW, live_failures=0, live_error_code=None)
    MarketSyncRuntime(repo, client, now=lambda: NOW + timedelta(seconds=30)).run_cycle()
    assert len(client.calls) == 1


@pytest.mark.parametrize("internal,expected,delay", [(False, "SOURCE_LIMIT", 21600), (True, "GAP", 300)])
def test_empty_history_has_visible_finite_retry_without_fake_candles(internal, expected, delay):
    repo, client = Repository(), Client()
    repo.internal = internal
    MarketSyncRuntime(repo, client, now=lambda: NOW).run_cycle()
    history = next(e[1] for e in repo.events if e[0] == "history")
    assert history["state"] == expected and history["delay"] == delay


def test_cycle_limits_latest_work_and_stop_is_respected():
    repo, client = Repository(25), Client()
    MarketSyncRuntime(repo, client, now=lambda: NOW).run_cycle()
    assert len(client.calls) == 18  # 16 live + two sequential bounded history pages
    MarketSyncRuntime(repo, client, now=lambda: NOW).run_cycle(stopped=lambda: True)
    assert len(client.calls) == 18


@pytest.mark.parametrize("header,minimum", [("123", 123), ("bad", 30), ("-10", 1), ("NaN", 30), ("inf", 30), ("1e100", 30)])
def test_gate_rate_limit_surface_and_no_transport_retries(header, minimum):
    client = GateClient(retries=0, timeout=(3, 7))
    assert client.session.get_adapter("https://").max_retries.total == 0
    client.session.get = lambda *a, **kw: SimpleNamespace(status_code=429, headers={"Retry-After": header})
    with pytest.raises(GateRateLimited) as exc:
        client.get_json("/spot/time")
    assert exc.value.retry_after == minimum


class Archive:
    def __init__(self, failure):
        self.failure=failure
        self.calls=[]

    def check_capacity(self): pass

    def fetch(self,instrument,timeframe,start,end):
        self.calls.append(instrument["symbol"])
        if instrument["symbol"]=="PAIR0_USDT":
            raise self.failure
        return [(start,100,102,99,101,3)], {"source":"GATE_MONTHLY_ARCHIVE"}


def archive_repository(count=2):
    repo=Repository(count)
    start=int(NOW.timestamp())-900000
    repo.history_plan=lambda s,now: {"gap":(start,start+60),"internal_gap":False}
    return repo


@pytest.mark.parametrize("reason",["UNSUPPORTED_ARCHIVE_SYMBOL","UNSUPPORTED_ARCHIVE_MARKET"])
def test_archive_format_error_is_distinct_slow_retry_without_starving_other_symbols(reason):
    repo,client=archive_repository(),Client()
    archive=Archive(ArchiveFormatError(reason))
    result=MarketSyncRuntime(repo,client,archive=archive,now=lambda:NOW).run_cycle(live=False)
    history=[event[1] for event in repo.events if event[0]=="history"]
    assert history[0]=={"state":"RETRYING","delay":86400,"error":reason}
    assert history[1]["state"]=="BACKFILLING" and history[1]["delay"]==1
    assert result["safe_error_code"]==reason and result["degraded"]
    assert result["inserted_bars"]==1 and result["history_pages"]==1
    assert archive.calls==["PAIR0_USDT","PAIR1_USDT"] and not client.calls
    assert not any(event[0]=="pause" for event in repo.events)
    # Only the failing symbol backs off across fresh runtime instances. Its live
    # data remains on the regular schedule, and healthy symbols keep backfilling.
    MarketSyncRuntime(repo,client,archive=archive,now=lambda:NOW+timedelta(seconds=61)).run_cycle()
    assert archive.calls==["PAIR0_USDT","PAIR1_USDT","PAIR1_USDT"]
    assert [call[0] for call in client.calls]==["PAIR0_USDT","PAIR1_USDT"]


def test_archive_429_pauses_provider_and_preserves_retry_after():
    repo,client=archive_repository(),Client()
    archive=Archive(GateRateLimited(321))
    result=MarketSyncRuntime(repo,client,archive=archive,now=lambda:NOW).run_cycle(live=False)
    history=next(event[1] for event in repo.events if event[0]=="history")
    assert history=={"state":"RETRYING","delay":321,"error":"MARKET_RATE_LIMITED"}
    assert result["rate_limited"] and result["safe_error_code"]=="MARKET_RATE_LIMITED"
    assert ("pause",321) in repo.events and archive.calls==["PAIR0_USDT"]
    MarketSyncRuntime(repo,client,archive=archive,now=lambda:NOW+timedelta(seconds=61)).run_cycle()
    assert archive.calls==["PAIR0_USDT"] and not client.calls


@pytest.mark.parametrize("failure,safe",[(ValueError("HISTORY_DISK_LOW"),"HISTORY_DISK_LOW"),(RuntimeError("https://secret.example"),"MARKET_HISTORY_FAILED")])
def test_other_archive_failures_keep_transient_retry_and_redacted_errors(failure,safe):
    repo,client=archive_repository(),Client()
    archive=Archive(failure)
    result=MarketSyncRuntime(repo,client,archive=archive,now=lambda:NOW).run_cycle(live=False)
    history=[event[1] for event in repo.events if event[0]=="history"]
    assert history[0]=={"state":"RETRYING","delay":60,"error":safe}
    assert history[1]["state"]=="BACKFILLING" and result["inserted_bars"]==1
    assert "secret" not in repr(repo.events)


class SparseRepository(Repository):
    def __init__(self,start,end,timeframe,step):
        super().__init__(1)
        identifier=self.pairs[0].instrument_id
        self.pairs=[MarketSubscription(identifier,"GATE","FUTURES_USDT","龙虾_USDT","USDT",timeframe)]
        self.states={(identifier,timeframe):self.states[(identifier,"1m")]}
        self.start,self.end,self.step=start,end,step
        self.present={}
        self.blocked=[]
        self.batches=[]
        self.single_defers=[]

    def history_plan(self,s,now):
        boundaries=[self.start-self.step,*sorted(self.present),self.end+self.step]
        gaps=[(left+self.step,right-self.step) for left,right in zip(boundaries,boundaries[1:],strict=False) if right-left>self.step]
        target=(self.end-self.start)//self.step+1
        return {"gap":eligible_gap(list(reversed(gaps)),self.blocked,self.step),"internal_gap":True,
            "target_from":datetime.fromtimestamp(self.start,timezone.utc),
            "missing_bars":target-len(self.present)}

    def upsert_final_bars(self,s,rows,**kwargs):
        count=len(self.present)
        for row in rows:
            self.present.setdefault(row[0],row)
        return len(self.present)-count

    def defer_history_ranges(self,s,ranges,now,**kwargs):
        self.batches.append((ranges,kwargs))
        self.blocked.extend(ranges)

    def defer_history(self,s,start,end,now,**kwargs):
        self.single_defers.append((start,end,kwargs))
        self.blocked.append((start,end))


def actual_archive(tmp_path,timestamps):
    payload=gzip.compress("".join(f"{ts},10,101,102,99,100\n" for ts in timestamps).encode())
    response=MagicMock(status_code=200,headers={})
    response.__enter__.return_value=response
    response.iter_content.return_value=iter([payload])
    session=MagicMock()
    session.get.return_value=response
    return GateHistoryArchive(tmp_path,session)


def test_sparse_history_expands_a_one_bar_gap_and_batches_450_proven_holes(tmp_path):
    start=int(datetime(2026,8,1,tzinfo=timezone.utc).timestamp())
    repo=SparseRepository(start,start+899*60,"1m",60)
    original=(start+898*60,200,202,199,201,6)
    repo.present[original[0]]=original
    assert repo.history_plan(repo.pairs[0],NOW)["gap"]==(start+899*60,start+899*60)
    archive=actual_archive(tmp_path,[start+i*60 for i in range(0,900,2)])
    result=MarketSyncRuntime(repo,Client(),archive=archive,now=lambda:NOW).run_cycle(live=False)
    assert result["history_pages"]==1 and result["inserted_bars"]==449
    assert repo.present[original[0]]==original and len(repo.present)==450
    assert len(repo.batches)==1 and len(repo.batches[0][0])==450
    assert repo.batches[0][1]=={"reason":"ARCHIVE_RANGE_EMPTY","delay":21600}
    assert not repo.single_defers and repo.history_plan(repo.pairs[0],NOW)["gap"] is None
    history=next(event[1] for event in repo.events if event[0]=="history")
    assert history=={"state":"SOURCE_LIMIT","delay":900}


def test_archive_expansion_does_not_go_before_listing_adjusted_target(tmp_path):
    month=int(datetime(2026,8,1,tzinfo=timezone.utc).timestamp())
    start=month+800*60
    repo=SparseRepository(start,month+899*60,"1m",60)
    archive=actual_archive(tmp_path,[month+i*60 for i in range(900)])
    result=MarketSyncRuntime(repo,Client(),archive=archive,now=lambda:NOW).run_cycle(live=False)
    assert result["inserted_bars"]==100 and min(repo.present)==start
    assert not repo.batches and not repo.single_defers


def test_runtime_empty_cross_month_does_not_defer_unqueried_previous_month(tmp_path):
    month=int(datetime(2026,6,1,tzinfo=timezone.utc).timestamp())
    repo=SparseRepository(month-898*900,month+900,"15m",900)
    archive=actual_archive(tmp_path,[month+i*300 for i in (0,2,3,4,6,7,8)])
    result=MarketSyncRuntime(repo,Client(),archive=archive,now=lambda:NOW).run_cycle(live=False)
    assert result["inserted_bars"]==0 and not repo.batches
    assert repo.single_defers==[(month,month+900,{"reason":"ARCHIVE_RANGE_EMPTY","delay":21600})]
    assert repo.history_plan(repo.pairs[0],NOW)["gap"]==(month-898*900,month-900)
    assert not repo.present


def test_legacy_empty_archive_metadata_still_clips_deferral_to_actual_month():
    month=int(datetime(2026,6,1,tzinfo=timezone.utc).timestamp())
    repo=SparseRepository(month-898*900,month+900,"15m",900)
    archive=SimpleNamespace(check_capacity=lambda:None,fetch=lambda *args:([],{"source":"GATE_MONTHLY_ARCHIVE"}))
    MarketSyncRuntime(repo,Client(),archive=archive,now=lambda:NOW).run_cycle(live=False)
    assert repo.single_defers[0][:2]==(month,month+900)


def test_postgres_batch_defers_are_bounded_single_transaction_and_single_executemany():
    pool=MagicMock()
    connection=pool.public_transaction.return_value.__enter__.return_value
    cursor=connection.cursor.return_value.__enter__.return_value
    repo=PostgresMarketIngestRepository(pool)
    subscription=Repository(1).pairs[0]
    ranges=[(i*120,i*120) for i in range(450)]
    repo.defer_history_ranges(subscription,ranges,NOW,reason="ARCHIVE_RANGE_EMPTY")
    pool.public_transaction.assert_called_once_with()
    connection.execute.assert_called_once()
    cursor.executemany.assert_called_once()
    sql,params=cursor.executemany.call_args.args
    assert "ON CONFLICT" in sql and len(params)==450
    assert params[0]==(subscription.instrument_id,"1m",0,0,NOW+timedelta(hours=6),"ARCHIVE_RANGE_EMPTY")
    assert params[-1][2:4]==ranges[-1]


@pytest.mark.parametrize("ranges",[
    [(i*120,i*120) for i in range(451)],[(0,54000)],[(1,60)],[(60,0)],
    [(0,60),(60,120)],[(0,0),(60,60)],[(120,120),(0,0)],[(True,60)],
])
def test_postgres_batch_rejects_unbounded_unaligned_or_overlapping_proofs_before_sql(ranges):
    pool=MagicMock()
    repo=PostgresMarketIngestRepository(pool)
    with pytest.raises(ValueError,match="HISTORY_DEFER_RANGE"):
        repo.defer_history_ranges(Repository(1).pairs[0],ranges,NOW,reason="ARCHIVE_RANGE_EMPTY")
    pool.public_transaction.assert_not_called()


def test_postgres_empty_batch_does_not_open_a_transaction():
    pool=MagicMock()
    PostgresMarketIngestRepository(pool).defer_history_ranges(
        Repository(1).pairs[0],[],NOW,reason="ARCHIVE_RANGE_EMPTY")
    pool.public_transaction.assert_not_called()
