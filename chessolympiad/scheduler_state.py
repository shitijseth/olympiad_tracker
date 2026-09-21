"""Tiny on-disk state for the live cron scheduler (chessolympiad.cli.tick,
invoked fresh every minute by scripts/scheduler.sh). Each `tick` call is a
new process with no memory of the last one, so cadences slower than one
minute (the 2-hour not-active fetch, the 5-minute active-phase sim, the
~22-hour full roster refresh) and the active-window idle timeout all need
something on disk to compare against.
"""

from __future__ import annotations

from pathlib import Path

STATE_DIR = Path(__file__).resolve().parents[1] / "data"


def _path(name: str) -> Path:
    return STATE_DIR / f".sched_{name}"


def due(name: str, interval_seconds: float, now: float) -> bool:
    """True if `interval_seconds` have passed since mark_done(name, ...)
    was last called (or it's never been called at all)."""
    f = _path(name)
    if not f.exists():
        return True
    try:
        last = float(f.read_text().strip())
    except ValueError:
        return True
    return (now - last) >= interval_seconds


def mark_done(name: str, now: float) -> None:
    _path(name).write_text(str(now))


def read_progress(name: str) -> tuple[int | None, float | None]:
    """(round_no, epoch) last recorded under `name`, or (None, None)."""
    f = _path(name)
    if not f.exists():
        return None, None
    try:
        round_str, epoch_str = f.read_text().strip().split()
        return int(round_str), float(epoch_str)
    except ValueError:
        return None, None


def write_progress(name: str, round_no: int, epoch: float) -> None:
    _path(name).write_text(f"{round_no} {epoch}")
