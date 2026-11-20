import copy
import importlib
from types import SimpleNamespace

from gatescope.quant.stage_environment import stage_environment_key
from gatescope.quant.structure_lifecycle import observe
from tests.unit.test_structure_lifecycle import candidate


def bars(count=150):
    return [{"ts": i*60, "open": 100+i, "high": 102+i, "low": 99+i, "close": 101+i, "volume": 10.}
            for i in range(count)]


def test_fixed_environment_uses_closed_prefix_and_requires_contiguous_warmup():
    rows = bars()
    assert stage_environment_key(rows, 60) == "se1:UP:NORMAL:NORMAL"
    assert stage_environment_key(rows[:109], 60) is None
    assert stage_environment_key(rows, 300) is None
    assert stage_environment_key(rows[:-5]+rows[-4:], 60) is None
    changed = copy.deepcopy(rows)
    changed[-1]["volume"] = 16
    assert stage_environment_key(changed, 60) == "se1:UP:NORMAL:HIGH"


def test_onset_environment_is_immutable_and_never_reads_future_candles():
    original, changed = bars(), bars()
    for bar in changed[111:]:
        for key in ("open", "high", "low", "close"):
            bar[key] *= 10
        bar["volume"] = 1_000_000
    states = []
    for rows in (original, changed):
        state = {"step_seconds": 60}
        observe(rows, 110, state, detector=lambda prefix: [candidate(prefix, expiry=8)])
        frozen = copy.deepcopy(state["records"]["a"]["stages"]["FORMING"])
        observe(rows, 111, state, detector=lambda prefix: [])
        assert state["records"]["a"]["stages"]["FORMING"] == frozen
        states.append(frozen)
    assert states[0] == states[1]
    assert states[0]["environment_key"] == "se1:UP:NORMAL:NORMAL"


def test_invalid_or_unclassifiable_environment_stays_unknown():
    rows = bars()
    rows[-1]["close"] = float("nan")
    assert stage_environment_key(rows, 60) is None
    assert stage_environment_key([{**row, "volume": 0} for row in bars()], 60) is None


def test_environment_migration_does_not_validate_scan_existing_history(monkeypatch):
    migration=importlib.import_module("migrations.versions.0051_stage_environment")
    statements=[]
    monkeypatch.setattr(migration,"op",SimpleNamespace(
        get_bind=lambda:SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),execute=statements.append))
    migration.upgrade()
    assert statements[0]=="ALTER TABLE market_structure_stages ADD COLUMN environment_key text"
    assert "ADD CONSTRAINT market_structure_stage_environment_key_valid" in statements[1]
    assert statements[1].endswith("NOT VALID")
    assert "VALIDATE CONSTRAINT" not in " ".join(statements)
