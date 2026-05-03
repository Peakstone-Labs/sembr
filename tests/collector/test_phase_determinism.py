"""SC#5 (a)(b): hash phase is deterministic and spreads feeds apart."""
from __future__ import annotations

import pytest

from sembr.collector.phase import derive_jitter_seconds, derive_phase_seconds


def test_phase_is_deterministic_across_calls() -> None:
    # SC#5(a): same feed.id always yields same phase.
    values = {derive_phase_seconds(42, 1800) for _ in range(100)}
    assert len(values) == 1


def test_phase_within_bounds() -> None:
    period = 1800
    for fid in range(0, 100):
        phase = derive_phase_seconds(fid, period)
        assert 0 <= phase < period


def test_distinct_feed_ids_distribute() -> None:
    # Cheap collision sanity: 50 feed_ids with 30-min period must produce
    # at least 40 distinct phase values (avg gap > 36s).
    period = 1800
    phases = {derive_phase_seconds(fid, period) for fid in range(50)}
    assert len(phases) >= 40


def test_jitter_within_bounds() -> None:
    assert derive_jitter_seconds(300) == 60  # min clamp
    assert derive_jitter_seconds(1800) == 60  # 1800//30 == 60
    assert derive_jitter_seconds(3600) == 120
    assert derive_jitter_seconds(86400) == 600  # max clamp (86400//30 = 2880 -> capped)


def test_phase_rejects_nonpositive_period() -> None:
    with pytest.raises(ValueError):
        derive_phase_seconds(1, 0)
    with pytest.raises(ValueError):
        derive_jitter_seconds(-5)
