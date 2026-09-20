"""Run a Monte Carlo forecast for one tournament, persist it to SQLite, and
write a human-readable markdown + CSV report.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path

from chessolympiad.data import db
from chessolympiad.simulate.loader import get_num_rounds, load_boundary_round, load_real_rounds, load_rosters, load_teams
from chessolympiad.simulate.tournament import run_monte_carlo, run_monte_carlo_parallel

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def _data_fingerprint(teams, rosters, num_rounds: int, iterations: int, real_rounds: dict, boundary_round) -> str:
    """Hash of everything that actually feeds the Monte Carlo run. Sampling
    noise means two runs on identical inputs never produce byte-identical
    forecasts, which would otherwise make a recurring live-update look
    "changed" (and get committed/pushed) every single cycle even when
    chess-results.com reported nothing new. Comparing this against the
    last stored run lets a no-op cycle skip resimulating entirely.
    """
    payload = {
        "iterations": iterations,
        "num_rounds": num_rounds,
        "teams": [dataclasses.asdict(t) for t in teams],
        "rosters": rosters,
        "real_rounds": {rd: dataclasses.asdict(data) for rd, data in real_rounds.items()},
        "boundary_round": boundary_round,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def simulate_and_store(
    tournament_id: str, iterations: int = 500, progress=None, max_workers: int = 1, notes: str | None = None
) -> int:
    conn = db.connect()
    try:
        teams = load_teams(conn, tournament_id)
        rosters = load_rosters(conn, tournament_id)
        num_rounds = get_num_rounds(conn, tournament_id)
        # complete_only=True: the forecast continues synthetic rounds on
        # top of whatever's replayed here, so a round must be wholly
        # decided (every team) before it's treated as "real" -- otherwise
        # a team still mid-match this round would get silently skipped and
        # re-paired incorrectly once synthetic rounds resume. The live
        # dashboard's own actual-standings display uses the unfiltered,
        # per-match version separately (export_artifact_data.py) and isn't
        # affected by this.
        real_rounds = load_real_rounds(conn, tournament_id, complete_only=True)
        as_of_round = max(real_rounds.keys()) if real_rounds else 0

        # The round currently in progress (published, not yet wholly
        # decided): its already-known board results get locked in as fixed
        # too, same as a completed round, so only the boards still
        # unplayed actually carry Monte Carlo uncertainty -- not the whole
        # round. Doesn't affect as_of_round (which stays "whole rounds
        # complete" for the reasons above); this is an independent input.
        boundary_round = load_boundary_round(conn, tournament_id)

        fingerprint = _data_fingerprint(teams, rosters, num_rounds, iterations, real_rounds, boundary_round)
        prior = conn.execute(
            "SELECT run_id, data_fingerprint FROM simulation_runs WHERE tournament_id = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (tournament_id,),
        ).fetchone()
        if prior is not None and prior["data_fingerprint"] == fingerprint:
            if progress:
                progress(f"{tournament_id}: no change since run {prior['run_id']}, skipping resimulation")
            return prior["run_id"]

        if max_workers and max_workers > 1:
            team_forecasts, player_forecasts = run_monte_carlo_parallel(
                teams, rosters, num_rounds, iterations,
                real_rounds=real_rounds, boundary_round=boundary_round, max_workers=max_workers, progress=progress,
            )
        else:
            team_forecasts, player_forecasts = run_monte_carlo(
                teams, rosters, num_rounds, iterations,
                real_rounds=real_rounds, boundary_round=boundary_round, progress=progress,
            )

        cur = conn.execute(
            "INSERT INTO simulation_runs (tournament_id, created_at, as_of_round, iterations, notes, data_fingerprint) "
            "VALUES (?,?,?,?,?,?)",
            (tournament_id, dt.datetime.utcnow().isoformat(), as_of_round, iterations, notes, fingerprint),
        )
        run_id = cur.lastrowid

        team_rows = []
        for tf in team_forecasts.values():
            team_rows.append(
                {
                    "run_id": run_id,
                    "team_no": tf.team_no,
                    "p_gold": tf.p_gold,
                    "p_silver": tf.p_silver,
                    "p_bronze": tf.p_bronze,
                    "p_any_medal": tf.p_any_medal,
                    "p_top8": tf.p_top8,
                    "p_top16": tf.p_top16,
                    "p_category_medal": tf.p_category_medal,
                    "expected_rank": tf.expected_rank,
                    "expected_match_pts": tf.expected_match_pts,
                    "rank_std": tf.rank_std,
                }
            )
        db.upsert(conn, "team_forecasts", team_rows, key_cols=["run_id", "team_no"])

        player_rows = []
        for pf in player_forecasts.values():
            player_rows.append(
                {
                    "run_id": run_id,
                    "team_no": pf.team_no,
                    "board_no": pf.board_no,
                    "player_key": pf.player_key,
                    "fide_id": pf.fide_id,
                    "name": pf.name,
                    "p_board_medal": pf.p_board_medal,
                    "expected_tpr": pf.expected_tpr,
                }
            )
        db.upsert(conn, "player_forecasts", player_rows, key_cols=["run_id", "team_no", "board_no", "player_key"])
        conn.commit()
        return run_id
    finally:
        conn.close()


def write_reports(tournament_id: str, run_id: int) -> tuple[Path, Path]:
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT tf.*, t.federation, t.team_name, t.rating_avg
            FROM team_forecasts tf JOIN teams t ON t.tournament_id = ? AND t.team_no = tf.team_no
            WHERE tf.run_id = ?
            ORDER BY tf.p_any_medal DESC, tf.expected_rank ASC
            """,
            (tournament_id, run_id),
        ).fetchall()
        tinfo = conn.execute(
            "SELECT name, as_of_round, iterations FROM tournaments t JOIN simulation_runs r ON r.tournament_id = t.tournament_id WHERE r.run_id = ?",
            (run_id,),
        ).fetchone()
        if tinfo is None:
            tinfo = conn.execute(
                "SELECT name FROM tournaments WHERE tournament_id = ?", (tournament_id,)
            ).fetchone()

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = REPORTS_DIR / f"{tournament_id}_forecast.csv"
        md_path = REPORTS_DIR / f"{tournament_id}_forecast.md"

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                ["rank_by_medal_prob", "federation", "team_name", "rating_avg", "p_gold", "p_silver", "p_bronze",
                 "p_any_medal", "p_category_medal", "p_top8", "p_top16", "expected_rank", "expected_match_pts", "rank_std"]
            )
            for i, r in enumerate(rows, start=1):
                w.writerow(
                    [i, r["federation"], r["team_name"], r["rating_avg"], f"{r['p_gold']:.4f}", f"{r['p_silver']:.4f}",
                     f"{r['p_bronze']:.4f}", f"{r['p_any_medal']:.4f}", f"{r['p_category_medal']:.4f}",
                     f"{r['p_top8']:.4f}", f"{r['p_top16']:.4f}", f"{r['expected_rank']:.2f}",
                     f"{r['expected_match_pts']:.2f}", f"{r['rank_std']:.2f}"]
                )

        lines = [f"# Forecast: {tournament_id}", ""]
        if tinfo:
            lines.append(f"as_of_round={tinfo['as_of_round'] if 'as_of_round' in tinfo.keys() else 0}, "
                          f"iterations={tinfo['iterations'] if 'iterations' in tinfo.keys() else '?'}")
            lines.append("")
        lines.append("| # | Fed | Team | Rtg | Gold% | Silver% | Bronze% | Any Medal% | Top8% | Exp.Rank |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(rows[:30], start=1):
            lines.append(
                f"| {i} | {r['federation']} | {r['team_name']} | {r['rating_avg']} | "
                f"{r['p_gold']*100:.1f} | {r['p_silver']*100:.1f} | {r['p_bronze']*100:.1f} | "
                f"{r['p_any_medal']*100:.1f} | {r['p_top8']*100:.1f} | {r['expected_rank']:.1f} |"
            )
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return csv_path, md_path
    finally:
        conn.close()
