"""Regression test for a real production bug in chessolympiad.cli.tick's
active-window idle timeout: a string of transient lichess network errors
(connection reset, read timeout -- not a real 429 rate limit) was treated
as equivalent to "confirmed no new results," which let IDLE_TIMEOUT trip
during a round that was very much still live, stranding it in the slow
2-hour not-active cadence for the rest of the day. The fix: only a poll
that fully succeeds and finds nothing new is allowed to advance the idle
clock toward the fallback -- a failed poll must be neutral, not treated as
silence.
"""

from __future__ import annotations

from chessolympiad.cli import _idle_clock_should_advance


def test_new_results_advance_the_clock():
    assert _idle_clock_should_advance(any_new=True, any_failure=False) is True


def test_a_failed_poll_advances_the_clock_even_with_nothing_new():
    # The exact regression: both sections' polls raised (network blip),
    # so any_new is False -- but that must NOT be read as "confirmed idle".
    assert _idle_clock_should_advance(any_new=False, any_failure=True) is True


def test_new_results_alongside_a_failure_still_advances_the_clock():
    assert _idle_clock_should_advance(any_new=True, any_failure=True) is True


def test_only_a_fully_successful_empty_poll_lets_the_deadline_approach():
    assert _idle_clock_should_advance(any_new=False, any_failure=False) is False
