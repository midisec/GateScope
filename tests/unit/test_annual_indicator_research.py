import hashlib
import json
import zlib
from uuid import uuid4

import pytest

from apps.api.research import BacktestRequest
from gatescope.quant.backtest import StrategySpec, features, run_backtest
from gatescope.quant.research_sequences import CHUNK_ROWS, ResearchSpool, decode_chunk, encode_chunk
from gatescope.quant.reporting import drawdown_analysis
from tests.unit.test_research_backtest import make_bars


@pytest.mark.parametrize("strategy", ["breakout", "momentum"])
def test_spilled_engine_matches_legacy_metrics_and_every_trade(strategy):
    bars = make_bars(600)
    spec = StrategySpec(strategy=strategy, direction="BOTH")
    original = run_backtest(bars, spec)
    with ResearchSpool() as spool:
        inputs = spool.sequence("bars", bars=True)
        for bar in bars:
            inputs.append(bar)
        compact = run_backtest(inputs, spec, sequence_factory=spool.sequence)
        assert compact["full"]["metrics"] == original["full"]["metrics"]
        assert compact["validation"] == original["validation"]
        assert compact["training"] == original["training"]
        assert compact["probability"] == original["probability"]
        assert compact["rule_attribution"] == original["rule_attribution"]
        assert compact["drawdown"]["summary"] == original["drawdown"]["summary"]
        assert list(spool.sequences["full_trades"]) == original["full"]["trades"]
        assert list(spool.sequences["full_curve"]) == original["full"]["equity_curve"]
        assert compact["input_bars"] == compact["indicator_series"] == []
        assert all(len(sequence.buffer) < CHUNK_ROWS for sequence in spool.sequences.values())
        assert len(json.dumps(compact)) < len(json.dumps(original)) / 10


@pytest.mark.parametrize(("strategy", "expected_digest"), [
    ("trend", "aa86e4dab261c4b41fec8a6e5825f071345b4ed7bad75359934b4a62491c1432"),
    ("breakout", "c2f28042c46cbb2686b97f8fcdb83f03bc8f2744b2b52b946bc8267c4f511234"),
    ("reversion", "c2f28042c46cbb2686b97f8fcdb83f03bc8f2744b2b52b946bc8267c4f511234"),
    ("pullback", "c2f28042c46cbb2686b97f8fcdb83f03bc8f2744b2b52b946bc8267c4f511234"),
    ("volume_breakout", "c2f28042c46cbb2686b97f8fcdb83f03bc8f2744b2b52b946bc8267c4f511234"),
    ("momentum", "4ea45061f64b67d9586fb361951bcf3ac9503e2f7d12aef171b86f4f8a90f354"),
    ("composite", "f24258d26e02121932bc55c33af14326d4ee9d4458f3d5cf638b22b98df4d107"),
])
def test_rolling_prefix_preserves_old_features_without_cross_chunk_reload(strategy, expected_digest):
    # Digests captured from the pre-deque implementation. Use maximum lookback
    # across two chunk boundaries and delayed inputs to protect both causality
    # and exact indicator values, not just the final headline trade metrics.
    bars = make_bars(1100, delay=2)
    extra = {"rule_ids": ["trend", "momentum"], "match_mode": "ANY"} if strategy == "composite" else {}
    spec = StrategySpec(strategy=strategy, direction="BOTH", lookback=100, slow_period=200, **extra)
    ordinary = features(bars, spec)
    assert hashlib.sha256(json.dumps(ordinary, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == expected_digest
    with ResearchSpool() as spool:
        inputs = spool.sequence("bars", bars=True)
        for bar in bars:
            inputs.append(bar)
        decode = inputs.decode
        decoded_rows = 0

        def counted_decode(value):
            nonlocal decoded_rows
            decoded_rows += 1
            return decode(value)

        inputs.decode = counted_decode
        signals = features(inputs, spec, output=spool.sequence("signals"))
        assert list(signals) == ordinary
        assert decoded_rows <= len(bars) + CHUNK_ROWS


def test_drawdown_recovery_scan_does_not_reload_old_peak_chunk_per_point():
    from datetime import datetime, timedelta, timezone
    begin = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = [{"time": (begin+timedelta(minutes=index)).isoformat(),
        "equity": 100-index*.05 if index < 600 else 50+(index-600)*.02} for index in range(1800)]
    ordinary = drawdown_analysis(rows)
    assert ordinary["summary"]["recovered"] is False
    assert ordinary["summary"]["trough_time"] == rows[600]["time"]
    with ResearchSpool() as spool:
        curve = spool.sequence("curve")
        for row in rows:
            curve.append(row)
        decoded_rows = 0

        def counted_decode(value):
            nonlocal decoded_rows
            decoded_rows += 1
            return value

        curve.decode = counted_decode
        result = drawdown_analysis(curve, output=spool.sequence("drawdown"))
        assert result["summary"] == ordinary["summary"]
        assert list(result["series"]) == ordinary["series"]
        # Two sequential scans plus bounded lookups for summary timestamps,
        # never O(points * chunk_rows) decodes of the first historical peak.
        assert decoded_rows <= len(rows)*3 + 6*CHUNK_ROWS


def test_more_than_5000_bars_uses_all_inputs_and_bounded_buffers():
    with ResearchSpool() as spool:
        inputs = spool.sequence("bars", bars=True)
        for bar in make_bars(5100):
            inputs.append(bar)
        report = run_backtest(inputs, StrategySpec(strategy="trend", direction="BOTH"),
            tested_hypotheses=51, sequence_factory=spool.sequence)
        assert report["bar_count"] == 5100
        assert report["detail_storage"]["counts"]["inputs"] == 5100
        assert len(spool.sequences["signals"]) == 5100
        assert report["range"]["from"] == inputs[0].open_time.isoformat()
        assert report["range"]["to"] == inputs[-1].close_time.isoformat()
        assert report["detail_storage"]["counts"]["curve"] == 5100-52+1
        assert all(len(sequence.buffer) < CHUNK_ROWS and len(sequence.cache) <= CHUNK_ROWS
            for sequence in spool.sequences.values())


def test_limits_allow_year_of_minutes_but_do_not_silently_enable_heavy_pattern_scan():
    request = BacktestRequest(instrument_id=uuid4(), timeframe="1m", bar_limit=525600)
    assert request.bar_limit == 525600
    combined = StrategySpec(strategy="composite", rule_ids=["trend", "breakout"])
    assert BacktestRequest(instrument_id=uuid4(), spec=combined, bar_limit=105120).bar_limit == 105120
    pattern = StrategySpec(strategy="composite", rule_ids=["pattern:double_bottom"])
    with pytest.raises(ValueError, match="年度形态"):
        BacktestRequest(instrument_id=uuid4(), spec=pattern, bar_limit=105120)
    with pytest.raises(ValueError):
        BacktestRequest(instrument_id=uuid4(), bar_limit=550001)


def test_chunks_roundtrip_exact_hashes_and_reject_tampering():
    original = [{"price": "1.230000", "confidence": .6574, "failed": True}]
    payload, digest = encode_chunk(original)
    assert decode_chunk(payload, digest) == original
    with pytest.raises(ValueError, match="DIGEST"):
        decode_chunk(payload, "0" * 64)
    with pytest.raises(ValueError):
        decode_chunk(payload+b"ignored suffix", digest)
    wrong_shape = b'{}'
    with pytest.raises(ValueError):
        decode_chunk(zlib.compress(wrong_shape), hashlib.sha256(wrong_shape).hexdigest())


def test_spill_private_temporary_files_cleaned_after_failure():
    path = None
    with pytest.raises(RuntimeError):
        with ResearchSpool() as spool:
            from pathlib import Path
            path = Path(spool.directory.name)
            sequence = spool.sequence("test")
            for index in range(CHUNK_ROWS+1):
                sequence.append({"index": index})
            assert sequence[-1] == {"index": CHUNK_ROWS}
            assert sequence[510:513] == [{"index": 510}, {"index": 511}, {"index": 512}]
            raise RuntimeError("calculation failed")
    assert path is not None and not path.exists()
