"""Real detector parity: historical prefixes and persisted live closes agree."""
import json
from datetime import datetime, timezone

import pytest

from gatescope.quant.pattern_study import WINDOW
from gatescope.quant.structure_lifecycle import observe
from tests.unit.test_structure_rules import pivot_case


@pytest.mark.parametrize("pattern", ["double_bottom", "elliott_correction_bull"])
def test_real_prefix_replay_and_restarted_live_follow_same_geometry(pattern):
    rows, _ = pivot_case(pattern)
    replay, live = {"step_seconds": 3600}, {"step_seconds": 3600}
    historical_decisions, live_decisions = [], []

    def decisions(journal):
        return [(j["candidate_id"], j["phase"], j["observed_time"],
                 j["record"]["reason"], j["record"]["latest"]["points"],
                 j["record"]["latest"]["lines"], j["record"]["latest"]["levels"],
                 j["record"]["latest"]["signal_direction"]) for j in journal]

    for index in range(19, len(rows)):
        timestamp = datetime.fromtimestamp(rows[index]["ts"] + 3600, timezone.utc).isoformat()
        historical_decisions.extend(decisions(observe(rows, index, replay,
            snapshot_at=timestamp, source_mode="REPLAY")))
        start = max(0, index - WINDOW + 1)
        visible = rows[start:index + 1]
        live_decisions.extend(decisions(observe(visible, len(visible) - 1, live,
            snapshot_at=timestamp, source_mode="RECORDED", index_offset=start)))
        if index % 7 == 0:
            live = json.loads(json.dumps(live))  # Database checkpoint/restart boundary.
    assert historical_decisions and historical_decisions == live_decisions
    assert any(r["pattern"] == pattern for r in replay["records"].values())
    for record in replay["records"].values():
        assert record["first_observed_time"] >= max(point["time"] for point in record["first"]["points"])
