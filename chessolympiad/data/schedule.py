"""Published FIDE round-start schedule for the live 2026 Olympiad -- both
sections play to the same schedule (http://chessolympiad2026.fide.com/schedule).
Used only to avoid polling chess-results.com for a round's results before
that round could possibly have any (games start on time per FIDE
regulations, and won't have any reported result in the first half hour
regardless), not to predict when a round finishes.
"""

from __future__ import annotations

import datetime as dt

# Only the two real live-2026 tournament_ids are gated by this schedule --
# anything else (historical calibration events, test fixtures) is treated
# as unrestricted below, since this table's dates would be meaningless for
# them.
_GATED_TOURNAMENT_IDS = {"2026-open", "2026-women"}

# All start times converted to UTC from the published "GMT+5" (Samarkand)
# times. September 22 is the event's one rest day -- no round 7 that day,
# round 7 resumes the 23rd. Round 11 starts earlier (11:00 local) than
# every other round (15:00 local).
_ROUND_START_UTC: dict[int, dt.datetime] = {
    1: dt.datetime(2026, 9, 16, 10, 0),
    2: dt.datetime(2026, 9, 17, 10, 0),
    3: dt.datetime(2026, 9, 18, 10, 0),
    4: dt.datetime(2026, 9, 19, 10, 0),
    5: dt.datetime(2026, 9, 20, 10, 0),
    6: dt.datetime(2026, 9, 21, 10, 0),
    7: dt.datetime(2026, 9, 23, 10, 0),
    8: dt.datetime(2026, 9, 24, 10, 0),
    9: dt.datetime(2026, 9, 25, 10, 0),
    10: dt.datetime(2026, 9, 26, 10, 0),
    11: dt.datetime(2026, 9, 27, 6, 0),
}

POLL_DELAY_AFTER_START = dt.timedelta(minutes=30)


def round_start_utc(tournament_id: str, round_no: int) -> dt.datetime | None:
    """None means "no published schedule for this tournament" -- callers
    should treat that as unrestricted (historical events, test fixtures)."""
    if tournament_id not in _GATED_TOURNAMENT_IDS:
        return None
    return _ROUND_START_UTC.get(round_no)


def round_polling_open(tournament_id: str, round_no: int, now: dt.datetime | None = None) -> bool:
    """Whether it's worth asking chess-results.com about this round yet."""
    start = round_start_utc(tournament_id, round_no)
    if start is None:
        return True
    return (now or dt.datetime.utcnow()) >= start + POLL_DELAY_AFTER_START
