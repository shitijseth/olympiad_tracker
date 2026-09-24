"""real_board_standings applies Appendix 2.III's eligibility + ranking to
actual (not simulated) per-player stats -- see test_board_tiebreak.py for
the ranking rule itself; this file is about wiring real data into it
correctly (grouping by board, merging team names, handling players with no
games at all).
"""

from __future__ import annotations

from chessolympiad.simulate.real_standings import real_board_standings


def _pstat(team_no, board_no, name, games, tpr, key_suffix=""):
    key = (team_no, board_no, f"{name}{key_suffix}")
    return key, {"teamNo": team_no, "boardNo": board_no, "fideId": None, "name": name, "games": games, "score": games / 2, "tpr": tpr}


TEAM_NAMES = {1: {"fed": "USA", "name": "United States"}, 2: {"fed": "IND", "name": "India"}}


def test_ranks_within_a_board_by_tpr_and_merges_team_info():
    entries = dict([
        _pstat(1, 1, "Alice", games=9, tpr=2700),
        _pstat(2, 1, "Bob", games=8, tpr=2650),
    ])
    out = real_board_standings(entries, TEAM_NAMES)
    board1 = out[1]
    assert [r["name"] for r in board1] == ["Alice", "Bob"]
    assert board1[0]["rank"] == 1 and board1[0]["fed"] == "USA" and board1[0]["team"] == "United States"
    assert board1[1]["rank"] == 2
    assert board1[0]["score"] == 4.5  # games=9 -> score=games/2 per the _pstat fixture


def test_boards_are_kept_separate():
    entries = dict([
        _pstat(1, 1, "Alice", games=9, tpr=2700),
        _pstat(1, 2, "Carol", games=9, tpr=2600),
    ])
    out = real_board_standings(entries, TEAM_NAMES)
    assert set(out.keys()) == {1, 2}
    assert out[1][0]["name"] == "Alice"
    assert out[2][0]["name"] == "Carol"


def test_players_with_no_games_are_excluded_entirely():
    entries = dict([_pstat(1, 1, "Alice", games=0, tpr=None)])
    out = real_board_standings(entries, TEAM_NAMES)
    assert out == {}


def test_ineligible_player_still_appears_but_unranked():
    entries = dict([
        _pstat(1, 1, "Alice", games=9, tpr=2700),
        _pstat(2, 1, "Newcomer", games=2, tpr=2900, key_suffix="2"),
    ])
    out = real_board_standings(entries, TEAM_NAMES)
    board1 = {r["name"]: r for r in out[1]}
    assert board1["Alice"]["eligible"] is True and board1["Alice"]["rank"] == 1
    assert board1["Newcomer"]["eligible"] is False and board1["Newcomer"]["rank"] is None


def test_reserve_board_five_is_just_another_board_no():
    entries = dict([_pstat(1, 5, "Reserve Rita", games=8, tpr=2500)])
    out = real_board_standings(entries, TEAM_NAMES)
    assert out[5][0]["name"] == "Reserve Rita"
    assert out[5][0]["rank"] == 1
