"""A recurring live-update re-simulates on every cron tick regardless of
whether chess-results.com reported anything new -- Monte Carlo sampling
noise then makes every cycle look "changed" and get committed/pushed even
during the many idle hours/day when nothing real happened. simulate_and_store
should skip resimulating (and reuse the prior run) when its inputs are
byte-for-byte unchanged since the last stored run.
"""

from __future__ import annotations

from chessolympiad.data import db
from chessolympiad.report.report import simulate_and_store

TID = "test-resim"


def _seed_tournament(db_path, num_rounds=3):
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tournaments (tournament_id, tnr, section, year, name, num_rounds) VALUES (?,?,?,?,?,?)",
            (TID, 1, "open", 2026, "Test Event", num_rounds),
        )
        for team_no in (1, 2):
            conn.execute(
                "INSERT INTO teams (tournament_id, team_no, federation, team_name, rating_avg, initial_rank) "
                "VALUES (?,?,?,?,?,?)",
                (TID, team_no, "USA", f"Team {team_no}", 2500, team_no),
            )
            for board_no in range(1, 5):
                conn.execute(
                    "INSERT INTO players (tournament_id, team_no, board_no, fide_id, name, rating) VALUES (?,?,?,?,?,?)",
                    (TID, team_no, board_no, team_no * 10 + board_no, f"P{team_no}.{board_no}", 2500),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_round_1(db_path, result="1-0"):
    conn = db.connect(db_path)
    try:
        for board_no in range(1, 5):
            conn.execute(
                """INSERT OR REPLACE INTO games
                (tournament_id, round, team_a_no, team_b_no, board_no, white_team_no,
                 white_fide_id, white_name, white_rating, black_fide_id, black_name, black_rating,
                 result, forfeit)
                VALUES (?,1,1,2,?,1,?,?,2500,?,?,2500,?,0)""",
                (
                    TID, board_no, 10 + board_no, f"P1.{board_no}",
                    20 + board_no, f"P2.{board_no}", result,
                ),
            )
        conn.execute("DELETE FROM matches WHERE tournament_id = ? AND round = 1", (TID,))
        conn.execute(
            "INSERT INTO matches (tournament_id, round, team_a_no, team_b_no, team_a_game_pts, team_b_game_pts, "
            "team_a_match_pts, team_b_match_pts) VALUES (?,1,1,2,4,0,2,0)",
            (TID,),
        )
        conn.commit()
    finally:
        conn.close()


def test_second_call_with_unchanged_data_reuses_the_prior_run(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_tournament(db_path)
    _insert_round_1(db_path)

    real_connect = db.connect
    import chessolympiad.report.report as report_mod

    # simulate_and_store calls db.connect() with no args -- point it at the temp DB.
    orig = report_mod.db.connect
    report_mod.db.connect = lambda *a, **k: real_connect(db_path)
    try:
        run_id_1 = simulate_and_store(TID, iterations=20)
        run_id_2 = simulate_and_store(TID, iterations=20)
        assert run_id_2 == run_id_1, "identical inputs should reuse the prior run, not resimulate"

        # A genuine data change (a new round result) must trigger a fresh run.
        conn = real_connect(db_path)
        conn.execute(
            """INSERT OR REPLACE INTO games
            (tournament_id, round, team_a_no, team_b_no, board_no, white_team_no,
             white_fide_id, white_name, white_rating, black_fide_id, black_name, black_rating,
             result, forfeit)
            VALUES (?,2,1,2,1,1,11,'P1.1',2500,21,'P2.1',2500,'1-0',0)""",
            (TID,),
        )
        conn.commit()
        conn.close()
        run_id_3 = simulate_and_store(TID, iterations=20)
        assert run_id_3 != run_id_1, "a real data change must produce a fresh run"

        # Different iteration count must also trigger a fresh run even
        # with otherwise-identical data.
        run_id_4 = simulate_and_store(TID, iterations=21)
        assert run_id_4 != run_id_3
    finally:
        report_mod.db.connect = orig
