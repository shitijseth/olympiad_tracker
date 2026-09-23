"""Regression test for the lichess rate-limit cooldown in tick(): a real
429 must stop tick() from hitting lichess again on the very next 1-minute
cycle -- it should back off for RATE_LIMIT_COOLDOWN seconds instead of
immediately retrying and re-tripping the limit every subsequent minute.
"""

from __future__ import annotations

import chessolympiad.cli as cli_mod
import chessolympiad.report.report as report_mod
import chessolympiad.schedule as schedule_mod
from chessolympiad import scheduler_state as state
from chessolympiad.data.lichess_client import RateLimited


def _stub_out_everything_except_lichess_sync(monkeypatch, lichess_calls, raise_rate_limited: bool):
    monkeypatch.setattr(schedule_mod, "scheduled_round_for", lambda now: 5)
    monkeypatch.setattr(cli_mod, "_both_sections_complete", lambda round_no: False)
    monkeypatch.setattr(cli_mod, "_deep_simulate_if_needed", lambda section, progress=None: False)
    monkeypatch.setattr(report_mod, "simulate_and_store", lambda *a, **k: 1)
    monkeypatch.setattr(report_mod, "write_reports", lambda *a, **k: (None, None))

    def fake_sync(section, progress):
        lichess_calls.append(section)
        if raise_rate_limited:
            raise RateLimited(f"lichess.org rate-limited -- back off ({section})")
        return 0

    monkeypatch.setattr(cli_mod, "_sync_lichess_only", fake_sync)


def test_a_real_rate_limit_starts_a_cooldown_that_skips_the_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    lichess_calls: list[str] = []
    _stub_out_everything_except_lichess_sync(monkeypatch, lichess_calls, raise_rate_limited=True)

    cli_mod.tick()
    assert lichess_calls == ["open", "women"], "first tick should try both sections and hit the rate limit"

    lichess_calls.clear()
    cli_mod.tick()
    assert lichess_calls == [], (
        "a tick within the cooldown window must not touch lichess at all -- "
        "retrying immediately just re-trips the same rate limit"
    )


def test_lichess_is_tried_again_once_the_cooldown_elapses(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    lichess_calls: list[str] = []
    _stub_out_everything_except_lichess_sync(monkeypatch, lichess_calls, raise_rate_limited=True)

    cli_mod.tick()
    lichess_calls.clear()

    # Fast-forward past the cooldown by backdating the recorded rate-limit timestamp.
    round_no, epoch = state.read_progress("active_progress")
    state.mark_done("lichess_cooldown", epoch - cli_mod.RATE_LIMIT_COOLDOWN - 1)

    cli_mod.tick()
    assert lichess_calls == ["open", "women"], "once the cooldown has elapsed, lichess should be tried again"
