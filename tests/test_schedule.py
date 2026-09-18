import datetime as dt

from chessolympiad.data import schedule


def test_unrestricted_for_non_2026_tournaments():
    # Historical/calibration events and test fixtures have no published
    # schedule here -- must never be gated by it.
    assert schedule.round_start_utc("2024-open", 1) is None
    assert schedule.round_polling_open("2024-open", 1, now=dt.datetime(2020, 1, 1)) is True
    assert schedule.round_polling_open("test-open", 1, now=dt.datetime(2020, 1, 1)) is True


def test_closed_before_round_start_plus_buffer():
    # Round 1 starts 2026-09-16 10:00 UTC.
    just_before_start = dt.datetime(2026, 9, 16, 9, 59)
    at_start = dt.datetime(2026, 9, 16, 10, 0)
    just_before_buffer_ends = dt.datetime(2026, 9, 16, 10, 29)
    assert schedule.round_polling_open("2026-open", 1, now=just_before_start) is False
    assert schedule.round_polling_open("2026-open", 1, now=at_start) is False
    assert schedule.round_polling_open("2026-open", 1, now=just_before_buffer_ends) is False


def test_open_once_buffer_elapses():
    at_buffer_end = dt.datetime(2026, 9, 16, 10, 30)
    well_after = dt.datetime(2026, 9, 16, 14, 0)
    assert schedule.round_polling_open("2026-open", 1, now=at_buffer_end) is True
    assert schedule.round_polling_open("2026-open", 1, now=well_after) is True


def test_rest_day_and_final_round_special_time():
    # No round is scheduled for Sep 22 (the rest day); round 7 resumes the
    # 23rd, not the 22nd.
    assert schedule.round_start_utc("2026-open", 7) == dt.datetime(2026, 9, 23, 10, 0)
    # Round 11 starts earlier (11:00 Samarkand = 06:00 UTC) than every
    # other round (15:00 Samarkand = 10:00 UTC).
    assert schedule.round_start_utc("2026-open", 11) == dt.datetime(2026, 9, 27, 6, 0)
    assert schedule.round_polling_open("2026-open", 11, now=dt.datetime(2026, 9, 27, 6, 29)) is False
    assert schedule.round_polling_open("2026-open", 11, now=dt.datetime(2026, 9, 27, 6, 30)) is True


def test_gates_both_2026_sections_identically():
    now = dt.datetime(2026, 9, 16, 9, 0)
    assert schedule.round_polling_open("2026-open", 1, now=now) is False
    assert schedule.round_polling_open("2026-women", 1, now=now) is False
