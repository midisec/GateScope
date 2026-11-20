from bisect import bisect_right as standard_bisect_right
import math
import random

import pytest

from gatescope.domain import history
from gatescope.domain.history import eligible_gap


def reference_gap(gaps, blocked, step, limit=900):
    """Original subtraction algorithm with empty on-grid ranges discarded."""
    normalized = [((a + step - 1) // step * step, b // step * step) for a, b in blocked]
    normalized = [(a, b) for a, b in normalized if a <= b]
    for start, end in gaps:
        if start > end:
            continue
        pieces = [(start, end)]
        for a, b in normalized:
            remaining = []
            for left, right in pieces:
                if b < left or a > right:
                    remaining.append((left, right))
                else:
                    if left < a:
                        remaining.append((left, a - step))
                    if right > b:
                        remaining.append((b + step, right))
            pieces = remaining
        if pieces:
            left, right = max(pieces, key=lambda p: p[1])
            return max(left, right - (limit - 1) * step), right
    return None


def test_random_small_inputs_match_reference_subtraction():
    rng = random.Random(1773138212)
    for _ in range(2500):
        step = rng.choice([1, 60, 300, 900, 3600, 14400, 86400])
        origin = rng.choice([-1000, 0, 1_773_138_212 // step]) * step
        gaps = []
        for _ in range(rng.randrange(15)):
            left, right = sorted((rng.randrange(-20, 60), rng.randrange(-20, 60)))
            gaps.append((origin + left * step, origin + right * step))
        blocked = []
        for _ in range(rng.randrange(20)):
            left, right = sorted((rng.randrange(-25 * step, 65 * step), rng.randrange(-25 * step, 65 * step)))
            blocked.append((origin + left, origin + right))
        limit = rng.choice([1, 2, 10, 30, 900])
        assert eligible_gap(iter(gaps), iter(blocked), step, limit) == reference_gap(gaps, blocked, step, limit)


@pytest.mark.parametrize("blocked,expected", [
    ([(120, 240), (180, 360), (360, 480), (540, 600)], (0, 60)),
    ([(540, 600), (360, 480), (180, 360), (120, 240)], (0, 60)),
    ([(60, 120), (240, 300), (420, 480)], (540, 600)),
    ([(60, 120), (240, 300), (420, 600)], (360, 360)),
    ([(60, 600), (60, 600), (-120, 0)], None),
    ([(121, 179)], (0, 600)),
    ([(121, 240), (181, 300), (301, 360), (361, 420), (421, 600)], (0, 120)),
    ([(-119, -61), (601, 659)], (0, 600)),
])
def test_overlapping_adjacent_and_unaligned_suppressions(blocked, expected):
    assert eligible_gap([(0, 600)], blocked, 60) == expected


def test_first_eligible_input_gap_order_and_cursor_laziness_are_preserved():
    consumed = []

    def cursor():
        for item in [(600, 660), (60, 180), (1200, 1800)]:
            consumed.append(item)
            yield item
        raise AssertionError("Gap cursor must not be materialized or consumed after finding a result")

    assert eligible_gap(cursor(), [(600, 660)], 60) == (60, 180)
    assert consumed == [(600, 660), (60, 180)]


def test_limit_keeps_the_latest_900_grid_slots_without_crossing_a_block():
    assert eligible_gap([(0, 100000 * 60)], [], 60) == ((100000 - 899) * 60, 100000 * 60)
    assert eligible_gap([(0, 100000 * 60)], [(99900 * 60, 100000 * 60)], 60) == ((99900 - 900) * 60, (99900 - 1) * 60)
    assert eligible_gap([(0, 600)], [(240, 300)], 60, limit=1) == (600, 600)


def test_thousands_of_sparse_blocked_holes_have_logarithmic_index_probes(monkeypatch):
    blocked_count = 6000
    blocked_iterations = 0
    gap_iterations = 0
    probe_counts = []

    def blocked_once():
        nonlocal blocked_iterations
        for index in range(blocked_count - 1, -1, -1):
            blocked_iterations += 1
            yield (index * 120, index * 120)

    def gap_cursor():
        nonlocal gap_iterations
        for index in range(blocked_count - 1, -1, -1):
            gap_iterations += 1
            yield (index * 120, index * 120)
        yield (-60, -60)
        raise AssertionError("Do not request another cursor row after the first usable gap")

    def measured_bisect(values, value):
        # A deterministic algorithmic bound, not a machine-speed/wall-clock test.
        assert len(values) == blocked_count
        probes = 0

        class CountedSequence:
            def __len__(self):
                return len(values)

            def __getitem__(self, index):
                nonlocal probes
                probes += 1
                return values[index]

        result = standard_bisect_right(CountedSequence(), value)
        probe_counts.append(probes)
        return result

    monkeypatch.setattr(history, "bisect_right", measured_bisect)
    assert eligible_gap(gap_cursor(), blocked_once(), 60) == (-60, -60)
    assert blocked_iterations == gap_iterations == blocked_count
    assert len(probe_counts) == blocked_count + 1
    assert max(probe_counts) <= math.ceil(math.log2(blocked_count + 1))


def test_thousands_of_adjacent_suppressions_merge_into_one_interval(monkeypatch):
    sizes = []

    def measured_bisect(values, value):
        sizes.append(len(values))
        return standard_bisect_right(values, value)

    monkeypatch.setattr(history, "bisect_right", measured_bisect)
    blocked = ((index * 60, index * 60) for index in reversed(range(9000)))
    assert eligible_gap([(-60, 9000 * 60 - 60)], blocked, 60) == (-60, -60)
    assert sizes == [1]


@pytest.mark.parametrize("step,limit", [(0, 900), (-1, 900), (60, 0), (60, -1)])
def test_nonpositive_grid_or_limit_is_rejected(step, limit):
    with pytest.raises(ValueError, match="must be positive"):
        eligible_gap([(0, 600)], [], step, limit)
