"""Regression tests for a real, currently-live bug in load_boundary_round:
it picked the LOWEST round with any missing result as "the round in
progress" to seed into the Monte Carlo model. A round with one
permanently-missing board (a walkover/forfeit chess-results.com never
posts a result for) satisfies that forever, so once one appeared, every
later day's genuinely-in-progress round was silently ignored -- the
simulation kept fully synthesizing the live round from scratch instead of
locking in its real, already-known results (a real result, like an actual
loss, produced no visible change to the forecast).

The fix: the boundary round must be the lowest incomplete round AFTER the
highest already-complete round, not just the lowest incomplete round.
See tests/test_boundary_round.py for apply_boundary_round's own tests
(what happens once a boundary round IS correctly identified) -- this file
is about identifying it in the first place.
"""

from __future__ import annotations

from chessolympiad.data import db
from chessolympiad.simulate.loader import load_boundary_round

TID = "2026-open"


def _setup(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO tournaments (tournament_id, tnr, section, year, num_rounds) VALUES (?,?,?,?,?)",
        (TID, 1, "open", 2026, 11),
    )
    conn.commit()
    return conn


def _insert_game(conn, round_no, team_a, team_b, board_no=1, result="1-0"):
    conn.execute(
        "INSERT INTO games (tournament_id, round, team_a_no, team_b_no, board_no, white_team_no, result) "
        "VALUES (?,?,?,?,?,?,?)",
        (TID, round_no, team_a, team_b, board_no, team_a, result),
    )


def test_a_stuck_earlier_round_no_longer_masks_a_later_in_progress_round(tmp_path):
    # The exact regression: round 1 has one permanently-missing board
    # (team 205 vs 102's board 2, in production -- reproduced here as
    # board 2 of a round-1 match never getting a result), rounds 2-5 are
    # fully decided, and round 6 is genuinely in progress.
    conn = _setup(tmp_path)
    _insert_game(conn, 1, 1, 2, board_no=1, result="1-0")
    _insert_game(conn, 1, 1, 2, board_no=2, result=None)  # the permanently stuck board
    for rd in (2, 3, 4, 5):
        _insert_game(conn, rd, 1, 3, result="1-0")
    _insert_game(conn, 6, 1, 4, board_no=1, result="1-0")
    _insert_game(conn, 6, 1, 4, board_no=2, result=None)  # still being played
    conn.commit()

    result = load_boundary_round(conn, TID)
    assert result is not None
    round_no, games = result
    assert round_no == 6, f"expected round 6 (the genuinely in-progress round), got {round_no}"
    assert len(games) == 2


def test_lowest_incomplete_round_is_still_picked_when_nothing_is_abandoned(tmp_path):
    conn = _setup(tmp_path)
    _insert_game(conn, 1, 1, 2, result="1-0")
    _insert_game(conn, 2, 1, 3, board_no=1, result="1-0")
    _insert_game(conn, 2, 1, 3, board_no=2, result=None)
    conn.commit()

    round_no, games = load_boundary_round(conn, TID)
    assert round_no == 2


def test_returns_none_when_every_published_round_is_complete(tmp_path):
    conn = _setup(tmp_path)
    _insert_game(conn, 1, 1, 2, result="1-0")
    _insert_game(conn, 2, 1, 3, result="1-0")
    conn.commit()

    assert load_boundary_round(conn, TID) is None


def test_returns_none_pre_event(tmp_path):
    conn = _setup(tmp_path)
    assert load_boundary_round(conn, TID) is None
