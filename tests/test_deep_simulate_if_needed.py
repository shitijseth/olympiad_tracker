"""_deep_simulate_if_needed must report back whether it actually
resimulated, not just log a message -- chessolympiad.cli.tick uses that
return value to decide whether there's something new worth publishing.
This guards a real gap found in production: the deep run used to only be
checked from inside tick()'s ACTIVE branch, so a round that completed and
closed the active window in the same tick it was first observed could
have its deep run silently skipped -- and even after moving the check to
run every tick regardless of phase, a silent bool-less return would have
meant the deep run happened but tick() never noticed to publish it.
"""

from __future__ import annotations

from chessolympiad.cli import _deep_simulate_if_needed
from chessolympiad.data import db

TID = "2026-open"  # _deep_simulate_if_needed looks up the section via cli.TOURNAMENTS, hardcoded to this id


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


def _insert_round(db_path, round_no):
    conn = db.connect(db_path)
    try:
        for board_no in range(1, 5):
            conn.execute(
                """INSERT OR REPLACE INTO games
                (tournament_id, round, team_a_no, team_b_no, board_no, white_team_no,
                 white_fide_id, white_name, white_rating, black_fide_id, black_name, black_rating,
                 result, forfeit)
                VALUES (?,?,1,2,?,1,?,?,2500,?,?,2500,'1-0',0)""",
                (TID, round_no, board_no, 10 + board_no, f"P1.{board_no}", 20 + board_no, f"P2.{board_no}"),
            )
        conn.execute("DELETE FROM matches WHERE tournament_id = ? AND round = ?", (TID, round_no))
        conn.execute(
            "INSERT INTO matches (tournament_id, round, team_a_no, team_b_no, team_a_game_pts, team_b_game_pts, "
            "team_a_match_pts, team_b_match_pts) VALUES (?,?,1,2,4,0,2,0)",
            (TID, round_no),
        )
        conn.commit()
    finally:
        conn.close()


def _patched(db_path, monkeypatch):
    real_connect = db.connect
    import chessolympiad.cli as cli_mod
    import chessolympiad.report.report as report_mod

    monkeypatch.setattr(cli_mod.db, "connect", lambda *a, **k: real_connect(db_path))
    monkeypatch.setattr(report_mod.db, "connect", lambda *a, **k: real_connect(db_path))


def test_returns_false_pre_event(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    _seed_tournament(db_path)
    _patched(db_path, monkeypatch)

    assert _deep_simulate_if_needed("open", iterations=20) is False


def test_returns_true_once_a_round_completes_then_false_on_repeat(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    _seed_tournament(db_path)
    _insert_round(db_path, 1)
    _patched(db_path, monkeypatch)

    assert _deep_simulate_if_needed("open", iterations=20) is True

    conn = db.connect(db_path)
    row = conn.execute(
        "SELECT notes, as_of_round FROM simulation_runs WHERE tournament_id=? ORDER BY run_id DESC LIMIT 1", (TID,)
    ).fetchone()
    conn.close()
    assert row["notes"] == "deep"
    assert row["as_of_round"] == 1

    # Same round, no new completion -- must no-op the second time.
    assert _deep_simulate_if_needed("open", iterations=20) is False


def test_returns_true_again_once_a_new_round_completes(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    _seed_tournament(db_path)
    _insert_round(db_path, 1)
    _patched(db_path, monkeypatch)

    assert _deep_simulate_if_needed("open", iterations=20) is True
    assert _deep_simulate_if_needed("open", iterations=20) is False

    _insert_round(db_path, 2)
    assert _deep_simulate_if_needed("open", iterations=20) is True
