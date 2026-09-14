import sqlite3

from chessolympiad.data.db import SCHEMA
from chessolympiad.simulate.real_standings import (
    compute_real_snapshot,
    real_player_stats,
    real_round_history,
    real_rounds_for_replay,
)
from chessolympiad.simulate.tournament import RealRoundData, TeamInfo


def _team(no, seed):
    return TeamInfo(team_no=no, federation=f"F{no}", team_name=f"Team{no}", initial_rank=seed, rating_avg=2500)


def _roster(team_no):
    return [{"board_no": b, "fide_id": team_no * 10 + b, "name": f"P{team_no}.{b}", "rating": 2500} for b in range(1, 5)]


def test_compute_real_snapshot_match_points_rank_and_player_stats():
    teams = [_team(1, 1), _team(2, 2), _team(3, 3), _team(4, 4)]
    rosters = {t.team_no: _roster(t.team_no) for t in teams}

    # Round 1: team1 beats team2 3-1 (board 1: team1 white wins); team3 beats team4 2.5-1.5.
    round1_games = [
        {"team_a_no": 1, "team_b_no": 2, "board_no": 1, "white_team_no": 1,
         "white_fide_id": 11, "white_name": "P1.1", "white_rating": 2500,
         "black_fide_id": 21, "black_name": "P2.1", "black_rating": 2500, "result": "1-0"},
        {"team_a_no": 1, "team_b_no": 2, "board_no": 2, "white_team_no": 2,
         "white_fide_id": 22, "white_name": "P2.2", "white_rating": 2500,
         "black_fide_id": 12, "black_name": "P1.2", "black_rating": 2500, "result": "0-1"},
        {"team_a_no": 1, "team_b_no": 2, "board_no": 3, "white_team_no": 1,
         "white_fide_id": 13, "white_name": "P1.3", "white_rating": 2500,
         "black_fide_id": 23, "black_name": "P2.3", "black_rating": 2500, "result": "1/2-1/2"},
        {"team_a_no": 1, "team_b_no": 2, "board_no": 4, "white_team_no": 2,
         "white_fide_id": 24, "white_name": "P2.4", "white_rating": 2500,
         "black_fide_id": 14, "black_name": "P1.4", "black_rating": 2500, "result": "0-1"},
    ]
    round1_matches = [
        {"team_a_no": 1, "team_b_no": 2, "team_a_game_pts": 3.0, "team_b_game_pts": 1.0,
         "team_a_match_pts": 2, "team_b_match_pts": 0, "is_bye": 0},
        {"team_a_no": 3, "team_b_no": 4, "team_a_game_pts": 2.5, "team_b_game_pts": 1.5,
         "team_a_match_pts": 2, "team_b_match_pts": 0, "is_bye": 0},
    ]

    # Round 2: team1 beats team3 2.5-1.5; team2 draws team4 2-2.
    round2_matches = [
        {"team_a_no": 1, "team_b_no": 3, "team_a_game_pts": 2.5, "team_b_game_pts": 1.5,
         "team_a_match_pts": 2, "team_b_match_pts": 0, "is_bye": 0},
        {"team_a_no": 2, "team_b_no": 4, "team_a_game_pts": 2.0, "team_b_game_pts": 2.0,
         "team_a_match_pts": 1, "team_b_match_pts": 1, "is_bye": 0},
    ]
    round2_games = [
        {"team_a_no": 1, "team_b_no": 3, "board_no": 1, "white_team_no": 1,
         "white_fide_id": 11, "white_name": "P1.1", "white_rating": 2500,
         "black_fide_id": 31, "black_name": "P3.1", "black_rating": 2500, "result": "1-0"},
    ]

    real_rounds = {
        1: RealRoundData(matches=round1_matches, games=round1_games),
        2: RealRoundData(matches=round2_matches, games=round2_games),
    }

    state, standings = compute_real_snapshot(teams, rosters, real_rounds, num_rounds=11)

    assert standings[1]["mp"] == 4  # two round wins
    assert standings[3]["mp"] == 2  # round 1 win, round 2 loss
    assert standings[2]["mp"] == 1  # round 1 loss, round 2 draw
    assert standings[4]["mp"] == 1  # round 1 loss, round 2 draw
    assert standings[1]["rank"] == 1  # clear leader on match points

    pstats = real_player_stats(state)
    # P1.1 (fide 11) played white on board 1 in both rounds and won both.
    key = next(k for k, v in pstats.items() if v["fideId"] == 11)
    assert pstats[key]["games"] == 2
    assert pstats[key]["score"] == 2.0
    assert pstats[key]["tpr"] is not None and pstats[key]["tpr"] > 2500  # scored above opponents' rating


def _seed_sqlite_matches_and_games(conn):
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO tournaments (tournament_id, tnr, section, year, name, num_rounds) VALUES "
        "('t1', 1, 'open', 2024, 'Test', 11)"
    )
    # Round 1: team 1 beats team 2; team 3 gets a bye.
    conn.execute(
        "INSERT INTO matches (tournament_id, round, team_a_no, team_b_no, team_a_game_pts, team_b_game_pts, "
        "team_a_match_pts, team_b_match_pts, is_bye) VALUES ('t1',1,1,2,3.0,1.0,2,0,0)"
    )
    conn.execute(
        "INSERT INTO matches (tournament_id, round, team_a_no, team_b_no, team_a_game_pts, team_b_game_pts, "
        "team_a_match_pts, team_b_match_pts, is_bye) VALUES ('t1',1,3,0,4.0,0.0,2,0,1)"
    )
    conn.execute(
        "INSERT INTO games (tournament_id, round, team_a_no, team_b_no, board_no, white_team_no, "
        "white_fide_id, white_name, white_rating, black_fide_id, black_name, black_rating, result) VALUES "
        "('t1',1,1,2,1,1,11,'P1.1',2600,21,'P2.1',2500,'1-0')"
    )
    conn.commit()


def test_real_round_history_reports_win_and_bye():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_sqlite_matches_and_games(conn)

    team_names = {1: {"fed": "AAA", "name": "Team A"}, 2: {"fed": "BBB", "name": "Team B"}, 3: {"fed": "CCC", "name": "Team C"}}
    history = real_round_history(conn, "t1", team_names)

    assert history[1][0]["result"] == "W"
    assert history[1][0]["opponentFed"] == "BBB"
    assert history[1][0]["boards"][0]["ownName"] == "P1.1"
    assert history[2][0]["result"] == "L"
    assert history[3][0]["result"] == "bye"
    assert history[3][0]["isBye"] is True


def test_real_rounds_for_replay_shapes_matches_and_boards():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_sqlite_matches_and_games(conn)

    replay = real_rounds_for_replay(conn, "t1")
    assert list(replay.keys()) == [1]
    by_team_a = {m["teamA"]: m for m in replay[1]}
    assert by_team_a[1]["teamB"] == 2
    assert by_team_a[1]["teamAWhiteOdd"] is True  # team 1 had white on board 1
    assert by_team_a[1]["boards"][0]["result"] == "1-0"
    assert by_team_a[3]["isBye"] is True
    assert by_team_a[3]["teamB"] is None
