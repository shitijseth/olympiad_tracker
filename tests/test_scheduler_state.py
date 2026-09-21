from __future__ import annotations

from chessolympiad import scheduler_state as state


def test_due_when_never_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    assert state.due("thing", 3600, now=1000.0) is True


def test_not_due_until_interval_elapses(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    state.mark_done("thing", now=1000.0)
    assert state.due("thing", 3600, now=1000.0 + 3599) is False
    assert state.due("thing", 3600, now=1000.0 + 3600) is True


def test_progress_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    assert state.read_progress("active") == (None, None)
    state.write_progress("active", 5, 1234.5)
    assert state.read_progress("active") == (5, 1234.5)
    state.write_progress("active", 6, 5678.0)  # overwrite on a new round
    assert state.read_progress("active") == (6, 5678.0)


def test_corrupt_state_file_treated_as_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    (tmp_path / ".sched_thing").write_text("not a number")
    assert state.due("thing", 3600, now=1000.0) is True
    (tmp_path / ".sched_progress").write_text("garbage")
    assert state.read_progress("progress") == (None, None)
