"""Fixed round-by-round schedule for the 46th Chess Olympiad (Samarkand
2026), used to gate the live cron pipeline's cadence (see scripts/scheduler.sh
and chessolympiad.cli.tick). All published round times are 15:00 GMT+5
except round 11's 11:00 GMT+5 (source: event schedule); Uzbekistan has no
DST, so a fixed +5 offset is exact for the whole event. Stored here in UTC
since that's what the rest of the codebase (created_at columns, cron's
`date -u`) already uses throughout.

2026-09-22 is the event's one rest day -- deliberately has no entry below.
scheduled_round_for doesn't need to special-case it: by the time the rest
day arrives, round 6 (2026-09-21) will already be complete, so the
completion + idle-timeout checks in chessolympiad.cli.tick naturally fall
back to "not active" for the whole rest day without this module needing to
know it's a rest day at all.
"""

from __future__ import annotations

import datetime as dt

_GMT5 = dt.timezone(dt.timedelta(hours=5))


def _start(date_str: str, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(
        dt.date.fromisoformat(date_str), dt.time(hour, minute), tzinfo=_GMT5
    ).astimezone(dt.timezone.utc)


ROUND_START_UTC: dict[int, dt.datetime] = {
    1: _start("2026-09-16", 15),
    2: _start("2026-09-17", 15),
    3: _start("2026-09-18", 15),
    4: _start("2026-09-19", 15),
    5: _start("2026-09-20", 15),
    6: _start("2026-09-21", 15),
    7: _start("2026-09-23", 15),
    8: _start("2026-09-24", 15),
    9: _start("2026-09-25", 15),
    10: _start("2026-09-26", 15),
    11: _start("2026-09-27", 11),
}
TOTAL_ROUNDS = 11


def scheduled_round_for(now: dt.datetime) -> int | None:
    """The highest-numbered round whose scheduled start has already passed
    as of `now` -- i.e. "which round's window could we be in", independent
    of whether that round has actually finished (chessolympiad.cli.tick
    decides that separately, from real completion + an idle timeout).
    None before round 1 starts.
    """
    started = [r for r, t in ROUND_START_UTC.items() if now >= t]
    return max(started) if started else None
