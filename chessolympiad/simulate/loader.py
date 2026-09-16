"""Load a tournament's teams/rosters/real-round-data out of SQLite into the
shapes simulate.tournament.run_monte_carlo expects.
"""

from __future__ import annotations

import sqlite3

from chessolympiad.model.adaptive_rating import effective_rating
from chessolympiad.simulate.tournament import RealRoundData, TeamInfo


def load_teams(conn: sqlite3.Connection, tournament_id: str) -> list[TeamInfo]:
    rows = conn.execute(
        "SELECT team_no, federation, team_name, initial_rank, rating_avg FROM teams WHERE tournament_id = ? ORDER BY initial_rank",
        (tournament_id,),
    ).fetchall()
    return [
        TeamInfo(
            team_no=r["team_no"], federation=r["federation"], team_name=r["team_name"],
            initial_rank=r["initial_rank"], rating_avg=r["rating_avg"] or 0.0,
        )
        for r in rows
    ]


def load_rosters(conn: sqlite3.Connection, tournament_id: str, adaptive: bool = True) -> dict[int, list[dict]]:
    """Rosters keyed by team_no, board order 1-5. If `adaptive`, each
    player's rating is blended with their real in-event performance so far
    (model.adaptive_rating) using data already synced into `games` --
    a no-op pre-event, since there are no games yet.
    """
    rows = conn.execute(
        "SELECT team_no, board_no, fide_id, name, rating FROM players WHERE tournament_id = ? ORDER BY team_no, board_no",
        (tournament_id,),
    ).fetchall()
    rosters: dict[int, list[dict]] = {}
    for r in rows:
        rosters.setdefault(r["team_no"], []).append(
            {"board_no": r["board_no"], "fide_id": r["fide_id"], "name": r["name"], "rating": r["rating"]}
        )

    if adaptive:
        perf = conn.execute(
            """
            SELECT white_fide_id AS fide_id, white_rating AS own_rating, black_rating AS opp_rating,
                   CASE result WHEN '1-0' THEN 1.0 WHEN '1/2-1/2' THEN 0.5 WHEN '0-1' THEN 0.0 END AS score
            FROM games WHERE tournament_id = ? AND result IS NOT NULL AND white_fide_id IS NOT NULL
            UNION ALL
            SELECT black_fide_id AS fide_id, black_rating AS own_rating, white_rating AS opp_rating,
                   CASE result WHEN '1-0' THEN 0.0 WHEN '1/2-1/2' THEN 0.5 WHEN '0-1' THEN 1.0 END AS score
            FROM games WHERE tournament_id = ? AND result IS NOT NULL AND black_fide_id IS NOT NULL
            """,
            (tournament_id, tournament_id),
        ).fetchall()
        agg: dict[int, list] = {}
        for r in perf:
            a = agg.setdefault(r["fide_id"], [0.0, 0, 0.0])
            a[0] += r["score"]
            a[1] += 1
            a[2] += r["opp_rating"] or 0

        for roster in rosters.values():
            for p in roster:
                if p["fide_id"] in agg and p["rating"]:
                    score, games, opp_sum = agg[p["fide_id"]]
                    avg_opp = opp_sum / games if games else None
                    p["rating"] = effective_rating(p["rating"], avg_opp, score, games)

    return rosters


def load_real_rounds(conn: sqlite3.Connection, tournament_id: str, complete_only: bool = False) -> dict[int, RealRoundData]:
    """Real rounds ingested so far, from `matches` (which itself populates
    per-match as each finishes -- see chessolympiad.data.sync._derive_matches
    -- so a round can appear here well before every team has played it).

    complete_only=True restricts to rounds where every published game has a
    result: required whenever this feeds something that then CONTINUES
    from this state into more (synthetic) rounds -- run_monte_carlo's
    remaining-rounds forecast and as_of_round itself, both in report.py --
    since a team missing from a partial round would silently get skipped
    this round and mis-paired once synthetic rounds resume. The live
    dashboard's actual-standings display (export_artifact_data.py) wants
    the opposite: whatever's decided right now, per-match, with no such
    continuation concern.

    Checked against `games`, not team coverage in `matches`: real events
    have withdrawn/no-show teams that never get a pairing again for the
    rest of the tournament, so requiring every ORIGINALLY-registered team
    to appear every round would make complete_only permanently false the
    moment that happens. A round with every currently-published game
    decided is exactly as "done" as chess-results.com considers it,
    regardless of who withdrew.
    """
    rounds = [
        r["round"]
        for r in conn.execute(
            "SELECT DISTINCT round FROM matches WHERE tournament_id = ? ORDER BY round", (tournament_id,)
        ).fetchall()
    ]
    if complete_only and rounds:
        rounds = [
            r["round"]
            for r in conn.execute(
                """
                SELECT round FROM games WHERE tournament_id = ? AND round IN ({})
                GROUP BY round
                HAVING SUM(CASE WHEN result IS NULL OR result = '' THEN 1 ELSE 0 END) = 0
                ORDER BY round
                """.format(", ".join("?" * len(rounds))),
                (tournament_id, *rounds),
            ).fetchall()
        ]
    out: dict[int, RealRoundData] = {}
    for rd in rounds:
        matches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM matches WHERE tournament_id = ? AND round = ?", (tournament_id, rd)
            ).fetchall()
        ]
        games = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM games WHERE tournament_id = ? AND round = ?", (tournament_id, rd)
            ).fetchall()
        ]
        out[rd] = RealRoundData(matches=matches, games=games)
    return out


def get_num_rounds(conn: sqlite3.Connection, tournament_id: str) -> int:
    row = conn.execute("SELECT num_rounds FROM tournaments WHERE tournament_id = ?", (tournament_id,)).fetchone()
    return row["num_rounds"] if row else 11
