"""Export forecast data for the GitHub Pages dashboard (docs/data/*.json,
fetched as plain static files -- see ui/artifact/dashboard.html's fetch
comment).

Produces a compact-but-complete JSON payload per section: team forecasts
(with nested rosters), per-board medal leaderboards, and aggregate field
stats.
"""

from __future__ import annotations

import json
from pathlib import Path

from chessolympiad.data import db
from chessolympiad.simulate.loader import load_real_rounds, load_rosters, load_teams
from chessolympiad.simulate.real_standings import (
    compute_real_snapshot,
    real_pairings,
    real_player_stats,
    real_round_history,
    real_rounds_for_replay,
)
from chessolympiad.simulate.round import _player_key

OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "artifact_export"


def _latest_run(conn, tournament_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM simulation_runs WHERE tournament_id=? ORDER BY run_id DESC LIMIT 1", (tournament_id,)
    ).fetchone()


def _medal_history(conn, tournament_id: str) -> dict[int, list[dict]]:
    """Each team's medal-probability forecast at every round milestone
    reached so far, for the Team Detail page's forecast-over-time chart.
    One point per distinct as_of_round value, taken from the latest run
    recorded at that milestone (many runs can share an as_of_round --
    every recurring cycle before the next round completes, plus a lot of
    historical manual testing at varying iteration counts -- the most
    recent one is the best-quality, most current estimate for that point).
    """
    rows = conn.execute(
        """
        SELECT sr.as_of_round AS round, tf.team_no, tf.p_gold, tf.p_silver, tf.p_bronze, tf.p_any_medal
        FROM simulation_runs sr
        JOIN team_forecasts tf ON tf.run_id = sr.run_id
        WHERE sr.tournament_id = ?
          AND sr.run_id = (
              SELECT MAX(sr2.run_id) FROM simulation_runs sr2
              WHERE sr2.tournament_id = sr.tournament_id AND sr2.as_of_round = sr.as_of_round
          )
        ORDER BY sr.as_of_round
        """,
        (tournament_id,),
    ).fetchall()
    history: dict[int, list[dict]] = {}
    for r in rows:
        history.setdefault(r["team_no"], []).append(
            {
                "round": r["round"],
                "pGold": round(r["p_gold"], 4),
                "pSilver": round(r["p_silver"], 4),
                "pBronze": round(r["p_bronze"], 4),
                "pAnyMedal": round(r["p_any_medal"], 4),
            }
        )
    return history


def export_section(conn, tournament_id: str) -> dict:
    t = conn.execute("SELECT * FROM tournaments WHERE tournament_id=?", (tournament_id,)).fetchone()
    run = _latest_run(conn, tournament_id)
    if run is None:
        raise SystemExit(f"No simulation run found for {tournament_id} -- run `simulate` first.")

    rosters: dict[int, list[dict]] = {}
    for r in conn.execute(
        "SELECT team_no, board_no, name, title, rating, federation, fide_id FROM players WHERE tournament_id=? ORDER BY team_no, board_no",
        (tournament_id,),
    ):
        rosters.setdefault(r["team_no"], []).append(
            {"board": r["board_no"], "name": r["name"], "title": r["title"], "rating": r["rating"], "fed": r["federation"], "fideId": r["fide_id"]}
        )

    # Real (not simulated) standings/history, populated per-match as soon
    # as each one is decided (unfiltered -- NOT complete_only=True) so the
    # live dashboard can show results well before a whole round wraps up.
    # Everything here is keyed off `matches` directly; asOfRound (below,
    # from the simulation_runs row) is a stricter "whole round N is fully
    # decided" signal the forecast itself needed to safely continue
    # synthetic rounds, and lags behind this in-progress data on purpose.
    real_rounds = load_real_rounds(conn, tournament_id)
    live_round = max(real_rounds.keys()) if real_rounds else 0
    real_standings: dict[int, dict] = {}
    round_history: dict[int, list[dict]] = {}
    real_pstats: dict[tuple[int, int, str], dict] = {}
    if real_rounds:
        typed_teams = load_teams(conn, tournament_id)
        snapshot_rosters = load_rosters(conn, tournament_id, adaptive=False)
        snapshot_state, real_standings = compute_real_snapshot(
            typed_teams, snapshot_rosters, real_rounds, num_rounds=t["num_rounds"]
        )
        team_names = {tm.team_no: {"fed": tm.federation, "name": tm.team_name} for tm in typed_teams}
        round_history = real_round_history(conn, tournament_id, team_names)
        real_pstats = real_player_stats(snapshot_state)

    # Attach each roster player's own real games/score/TPR so far -- not
    # just the per-board top-15 medal-chance list below, which only ever
    # shows the handful of players currently forecast to contend for that
    # board's medal, not "my team's own players." Keyed the same way
    # real_pstats itself is (team_no, board_no, player identity) so a
    # reserve who subs in accrues their own record at whatever board they
    # actually played, not the nominal roster board of whoever they
    # replaced.
    for team_no, roster in rosters.items():
        for p in roster:
            key = (team_no, p["board"], _player_key(p["fideId"], p["name"]))
            real = real_pstats.get(key)
            if real and real["games"] > 0:
                p["actualGames"] = real["games"]
                p["actualScore"] = real["score"]
                p["actualTpr"] = real["tpr"]

    medal_history = _medal_history(conn, tournament_id)
    teams = []
    for r in conn.execute(
        """
        SELECT tf.*, t.federation, t.team_name, t.rating_avg, t.captain, t.initial_rank
        FROM team_forecasts tf JOIN teams t ON t.tournament_id=? AND t.team_no=tf.team_no
        WHERE tf.run_id=? ORDER BY tf.p_any_medal DESC
        """,
        (tournament_id, run["run_id"]),
    ):
        standing = real_standings.get(r["team_no"])
        team_round_results = round_history.get(r["team_no"], [])
        teams.append({
            "no": r["team_no"], "fed": r["federation"], "name": r["team_name"],
            "rtg": r["rating_avg"], "captain": r["captain"], "seed": r["initial_rank"],
            "pGold": round(r["p_gold"], 4), "pSilver": round(r["p_silver"], 4), "pBronze": round(r["p_bronze"], 4),
            "pAnyMedal": round(r["p_any_medal"], 4), "pCatMedal": round(r["p_category_medal"], 4),
            "pTop8": round(r["p_top8"], 4), "pTop16": round(r["p_top16"], 4),
            "expRank": round(r["expected_rank"], 1), "expMp": round(r["expected_match_pts"], 1),
            "rankStd": round(r["rank_std"], 1),
            "roster": rosters.get(r["team_no"], []),
            # Real results so far -- absent/None pre-event (as_of_round=0).
            # actualMp is chess-results.com's own "TB1" column (it's also
            # the primary sort key, ahead of any real tiebreak); actualTb2/
            # 3/4 are its TB2/TB3/TB4 -- see chessolympiad.simulate.tiebreak
            # for the exact Annex 2.I formulas (verified byte-for-byte
            # against the live chess-results.com round-5 Open standings).
            "actualMp": standing["mp"] if standing else None,
            "actualRank": standing["rank"] if standing else None,
            "actualTb2": standing["tb"][0] if standing else None,
            "actualTb3": standing["tb"][1] if standing else None,
            "actualTb4": standing["tb"][2] if standing else None,
            "actualGamePts": round(sum(rr["ownGamePts"] for rr in team_round_results), 1) if team_round_results else None,
            "roundResults": team_round_results,
            "medalHistory": medal_history.get(r["team_no"], []),
        })

    boards: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT pf.*, t.federation, t.team_name FROM player_forecasts pf
        JOIN teams t ON t.tournament_id=? AND t.team_no=pf.team_no
        WHERE pf.run_id=? ORDER BY pf.board_no, pf.p_board_medal DESC
        """,
        (tournament_id, run["run_id"]),
    ):
        key = str(row["board_no"])
        lst = boards.setdefault(key, [])
        if len(lst) < 15:
            entry = {
                "fed": row["federation"], "team": row["team_name"], "name": row["name"],
                "tpr": round(row["expected_tpr"]), "pMedal": round(row["p_board_medal"], 4),
            }
            real = real_pstats.get((row["team_no"], row["board_no"], row["player_key"]))
            if real and real["games"] > 0:
                entry["actualGames"] = real["games"]
                entry["actualScore"] = real["score"]
                entry["actualTpr"] = real["tpr"]
            lst.append(entry)

    ratings = [r["rating"] for r in conn.execute(
        "SELECT rating FROM players WHERE tournament_id=? AND rating IS NOT NULL AND rating>0", (tournament_id,)
    )]
    fed_counts: dict[str, int] = {}
    for r in conn.execute("SELECT federation FROM teams WHERE tournament_id=?", (tournament_id,)):
        fed_counts[r["federation"]] = fed_counts.get(r["federation"], 0) + 1

    bins = [0] * 12
    lo, hi = 1000, 2900
    width = (hi - lo) / len(bins)
    for r in ratings:
        idx = min(len(bins) - 1, max(0, int((r - lo) // width)))
        bins[idx] += 1

    return {
        "tournamentId": tournament_id,
        "name": t["name"],
        "numRounds": t["num_rounds"],
        "numTeams": len(teams),
        "generatedAt": run["created_at"],
        "asOfRound": run["as_of_round"],
        # The round the live tables should currently show, updated as soon
        # as any match in it is decided -- see the comment above real_rounds.
        # asOfRound above only advances once that whole round is finished
        # (the forecast needs that guarantee; the live display doesn't).
        "liveRound": live_round,
        "iterations": run["iterations"],
        "teams": teams,
        "boards": boards,
        "realRounds": real_rounds_for_replay(conn, tournament_id) if real_rounds else {},
        # Published pairings, independent of realRounds -- populated as soon
        # as chess-results.com posts a round's board assignments, even
        # before any result comes in (see real_pairings' docstring). Used
        # only by the Rounds display page; realRounds above stays the
        # sole source for asOfRound/Simulate-replay/live standings.
        "pairings": real_pairings(conn, tournament_id),
        "stats": {
            "avgRating": round(sum(ratings) / len(ratings)) if ratings else None,
            "minRating": min(ratings) if ratings else None,
            "maxRating": max(ratings) if ratings else None,
            "ratingHistogram": {"lo": lo, "hi": hi, "binWidth": width, "bins": bins},
            "fedCounts": dict(sorted(fed_counts.items(), key=lambda kv: -kv[1])[:20]),
            "totalFederations": len(fed_counts),
        },
    }


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    try:
        for tid in ["2026-open", "2026-women"]:
            payload = export_section(conn, tid)
            path = OUT_DIR / f"{tid}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            print(f"{tid}: {len(teams := payload['teams'])} teams, "
                  f"{sum(len(v) for v in payload['boards'].values())} board entries, "
                  f"{path.stat().st_size / 1024:.1f} KiB -> {path}")
    finally:
        conn.close()
