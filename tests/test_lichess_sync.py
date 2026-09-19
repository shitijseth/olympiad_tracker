"""Everything here is mocked -- no real network calls to lichess.org."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from chessolympiad.data import db
from chessolympiad.data import lichess_sync as ls
from chessolympiad.data.lichess_client import BroadcastRound

TID = "test-open"
SECTION = "open"


def _game(white_team, black_team, white_name, black_name, result="1-0", white_fide=None, black_fide=None):
    return {
        "round": 1,
        "white_name": white_name,
        "black_name": black_name,
        "white_team_name": white_team,
        "black_team_name": black_team,
        "white_fide_id": white_fide,
        "black_fide_id": black_fide,
        "white_rating": 2600,
        "black_rating": 2600,
        "result": result,
    }


# ---------------- pure-function tests (no DB/network) ----------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("Bosnia & Herzegovina", "Bosnia and Herzegovina"),
        ("Trinidad and Tobago", "Trinidad & Tobago"),
        ("Cote d’Ivoire", "Cote d'Ivoire"),
        ("  Uzbekistan ", "uzbekistan"),
    ],
)
def test_normalize_team_name_matches_known_variants(a, b):
    assert ls._normalize_team_name(a) == ls._normalize_team_name(b)


def test_group_into_games_rows_assigns_boards_by_position():
    raw = [
        _game("Uzbekistan", "Turkiye", "P1", "P2"),
        _game("Turkiye", "Uzbekistan", "P3", "P4"),
        _game("Uzbekistan", "Turkiye", "P5", "P6"),
        _game("USA", "India", "P7", "P8"),  # a second, separate match right after
    ]
    teams = {"uzbekistan": 1, "turkiye": 2, "usa": 3, "india": 4}
    rows = ls._group_into_games_rows(raw, teams, TID, round_no=4)

    uzb_tur = [r for r in rows if {r["team_a_no"], r["team_b_no"]} == {1, 2}]
    assert [r["board_no"] for r in uzb_tur] == [1, 2, 3]
    assert {r["team_a_no"] for r in uzb_tur} == {1}  # team_a/b stay fixed across the group
    assert {r["team_b_no"] for r in uzb_tur} == {2}

    usa_ind = [r for r in rows if {r["team_a_no"], r["team_b_no"]} == {3, 4}]
    assert len(usa_ind) == 1
    assert usa_ind[0]["board_no"] == 1


def test_group_into_games_rows_alternating_colors_track_white_team_no():
    raw = [
        _game("Uzbekistan", "Turkiye", "P1", "P2"),  # board 1: UZB white
        _game("Turkiye", "Uzbekistan", "P3", "P4"),  # board 2: TUR white
    ]
    teams = {"uzbekistan": 1, "turkiye": 2}
    rows = ls._group_into_games_rows(raw, teams, TID, round_no=4)
    assert rows[0]["white_team_no"] == 1
    assert rows[1]["white_team_no"] == 2


def test_group_into_games_rows_skips_unmatched_team_name_without_crashing():
    raw = [_game("Neverland", "Turkiye", "P1", "P2")]
    teams = {"turkiye": 2}  # "Neverland" not in our teams table
    rows = ls._group_into_games_rows(raw, teams, TID, round_no=4)
    assert rows == []


# ---------------- DB-backed tests (network mocked) ----------------


@pytest.fixture
def patched(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO tournaments (tournament_id, tnr, section, year, name, num_rounds) VALUES (?,?,?,?,?,?)",
        (TID, 1, SECTION, 2026, "Test", 5),
    )
    for team_no, name in [(1, "Uzbekistan"), (2, "Turkiye")]:
        conn.execute(
            "INSERT INTO teams (tournament_id, team_no, federation, team_name, initial_rank) VALUES (?,?,?,?,?)",
            (TID, team_no, "XXX", name, team_no),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(ls.lc, "BROADCAST_IDS", {"open": ["bcastA", "bcastB"], "women": ["bcastC"]})
    fetch_rounds = MagicMock(return_value=[])
    fetch_games = MagicMock(return_value=[])
    monkeypatch.setattr(ls.lc, "fetch_broadcast_rounds", fetch_rounds)
    monkeypatch.setattr(ls.lc, "fetch_round_games", fetch_games)

    return db.connect(db_path), fetch_rounds, fetch_games


def test_sync_round_writes_games_and_derives_matches(patched):
    conn, fetch_rounds, fetch_games = patched
    fetch_rounds.side_effect = lambda bid: (
        [BroadcastRound(id="r1", number=1, finished=True)] if bid == "bcastA" else []
    )
    fetch_games.return_value = [
        _game("Uzbekistan", "Turkiye", "P1", "P2", "1-0"),
        _game("Turkiye", "Uzbekistan", "P3", "P4", "0-1"),
    ]

    written = ls.sync_round_from_lichess(conn, TID, SECTION, round_no=1)
    assert written == 2

    games = conn.execute("SELECT * FROM games WHERE tournament_id=? AND round=1", (TID,)).fetchall()
    assert len(games) == 2
    match = conn.execute("SELECT * FROM matches WHERE tournament_id=? AND round=1", (TID,)).fetchone()
    assert match is not None
    assert match["team_a_match_pts"] + match["team_b_match_pts"] == 2  # a decisive 2-0 match


def test_round_map_is_cached_not_refetched_once_known(patched):
    conn, fetch_rounds, fetch_games = patched
    fetch_rounds.side_effect = lambda bid: (
        [BroadcastRound(id="r1", number=1, finished=True)] if bid == "bcastA" else []
    )
    fetch_games.return_value = [_game("Uzbekistan", "Turkiye", "P1", "P2")]

    ls.sync_round_from_lichess(conn, TID, SECTION, round_no=1)
    assert fetch_rounds.call_count == 2  # both sub-broadcasts probed once, round map now cached

    fetch_rounds.reset_mock()
    # Round 1 is not locally complete (only 1/4 boards, no explicit
    # completeness here since this fixture never fills a full match) --
    # use a round number no broadcast has, so the *round map* lookup is
    # the thing under test, not the completeness short-circuit.
    ls.sync_round_from_lichess(conn, TID, SECTION, round_no=1)
    fetch_rounds.assert_not_called()  # round 1's ID is already cached for both sub-broadcasts


def test_sync_tournament_stops_at_first_unavailable_round(patched):
    conn, fetch_rounds, fetch_games = patched
    known_rounds = {
        "bcastA": [BroadcastRound(id="r1", number=1, finished=True), BroadcastRound(id="r2", number=2, finished=False)],
        "bcastB": [BroadcastRound(id="r1b", number=1, finished=True), BroadcastRound(id="r2b", number=2, finished=False)],
    }
    fetch_rounds.side_effect = lambda bid: known_rounds[bid]

    def games_for(round_id):
        if round_id in ("r1", "r1b"):
            return [_game("Uzbekistan", "Turkiye", "P1", "P2", "1-0"), _game("Turkiye", "Uzbekistan", "P3", "P4", "0-1")]
        if round_id in ("r2", "r2b"):
            return [_game("Uzbekistan", "Turkiye", "P5", "P6", "*")]  # round 2 started, nothing decided
        return []

    fetch_games.side_effect = games_for

    total = ls.sync_tournament_from_lichess(conn, TID, SECTION, num_rounds=5)
    assert total > 0
    rows = conn.execute("SELECT DISTINCT round FROM games WHERE tournament_id=?", (TID,)).fetchall()
    assert sorted(r["round"] for r in rows) == [1, 2]  # never attempted round 3+ -- no sub-broadcast has reached it


def test_second_call_skips_a_now_fully_decided_round(patched):
    conn, fetch_rounds, fetch_games = patched
    fetch_rounds.side_effect = lambda bid: (
        [BroadcastRound(id="r1", number=1, finished=True)] if bid == "bcastA" else []
    )
    fetch_games.return_value = [
        _game("Uzbekistan", "Turkiye", "P1", "P2", "1-0"),
        _game("Turkiye", "Uzbekistan", "P3", "P4", "0-1"),
    ]
    ls.sync_round_from_lichess(conn, TID, SECTION, round_no=1)

    fetch_games.reset_mock()
    written = ls.sync_round_from_lichess(conn, TID, SECTION, round_no=1)
    assert written == 0
    fetch_games.assert_not_called()  # round 1 is fully decided locally -- _complete_rounds short-circuits
