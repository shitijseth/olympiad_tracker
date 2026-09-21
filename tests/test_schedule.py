"""Correctness checks for the fixed round calendar -- a scheduling bug here
silently mispaces the live cron pipeline (wrong cadence at the wrong time),
so these pin the exact boundaries rather than just spot-checking one point
per round.
"""

from __future__ import annotations

import datetime as dt

from chessolympiad.schedule import ROUND_START_UTC, TOTAL_ROUNDS, scheduled_round_for


def test_all_eleven_rounds_present():
    assert set(ROUND_START_UTC.keys()) == set(range(1, TOTAL_ROUNDS + 1))


def test_nothing_scheduled_before_round_one():
    just_before = ROUND_START_UTC[1] - dt.timedelta(seconds=1)
    assert scheduled_round_for(just_before) is None


def test_round_becomes_active_exactly_at_its_start_time():
    assert scheduled_round_for(ROUND_START_UTC[5]) == 5
    assert scheduled_round_for(ROUND_START_UTC[5] - dt.timedelta(seconds=1)) == 4


def test_rest_day_reports_the_last_round_that_started():
    # 2026-09-22 is the rest day; round 6 started 2026-09-21, round 7 not
    # until 2026-09-23 -- scheduled_round_for should still report 6 (it's
    # cli.tick's completion/idle-timeout check that turns this into "not
    # active", not this function).
    rest_day_noon = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)
    assert scheduled_round_for(rest_day_noon) == 6


def test_round_eleven_start_time_matches_the_earlier_11am_slot():
    # Round 11 starts 11:00 GMT+5 = 06:00 UTC, unlike every other round's
    # 15:00 GMT+5 = 10:00 UTC.
    assert ROUND_START_UTC[11].astimezone(dt.timezone.utc).time() == dt.time(6, 0)
    assert ROUND_START_UTC[10].astimezone(dt.timezone.utc).time() == dt.time(10, 0)


def test_round_start_times_are_strictly_increasing():
    ordered = [ROUND_START_UTC[r] for r in sorted(ROUND_START_UTC)]
    assert ordered == sorted(ordered)
    assert len(set(ordered)) == len(ordered)


def test_far_future_still_resolves_to_last_round():
    far_future = ROUND_START_UTC[11] + dt.timedelta(days=365)
    assert scheduled_round_for(far_future) == 11
