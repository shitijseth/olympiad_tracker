"""Blind backtest: replay a finished historical Olympiad from Round 1 using
only pre-event (round-1) ratings, and score the simulator's forecast
against what actually happened. This is the calibration check -- if this
doesn't look sane, the 2026 forecast shouldn't be trusted.

Metrics:
  - Brier score for "finishes in the top 3" (medal) -- lower is better, 0 is perfect.
  - Spearman rank correlation between simulated expected_rank and the real
    final rank -- higher (closer to 1) is better.
"""

from __future__ import annotations

from dataclasses import dataclass

from chessolympiad.simulate.loader import load_teams
from chessolympiad.simulate.tiebreak import RoundRecord, compute_tiebreaks, rank_teams
from chessolympiad.simulate.tournament import TeamInfo, run_monte_carlo


def build_rosters_from_round1(conn, tournament_id: str) -> dict[int, list[dict]]:
    """Reconstruct each team's board-1..4 pre-event roster from whichever
    early round first shows that team playing (round 1 normally; a later
    round if a team had a round-1 bye/no-show)."""
    rosters: dict[int, list[dict]] = {}
    rounds = [
        r["round"]
        for r in conn.execute(
            "SELECT DISTINCT round FROM games WHERE tournament_id = ? ORDER BY round", (tournament_id,)
        ).fetchall()
    ]
    for rd in rounds:
        rows = conn.execute(
            "SELECT team_a_no, team_b_no, board_no, white_team_no, white_name, white_rating, black_name, black_rating "
            "FROM games WHERE tournament_id = ? AND round = ?",
            (tournament_id, rd),
        ).fetchall()
        for r in rows:
            white_team = r["white_team_no"]
            black_team = r["team_b_no"] if white_team == r["team_a_no"] else r["team_a_no"]
            for team_no, name, rating in ((white_team, r["white_name"], r["white_rating"]), (black_team, r["black_name"], r["black_rating"])):
                if team_no is None or not rating:
                    continue
                roster = rosters.setdefault(team_no, [])
                if not any(p["board_no"] == r["board_no"] for p in roster):
                    roster.append({"board_no": r["board_no"], "fide_id": None, "name": name, "rating": rating})
        if rounds.index(rd) >= 2 and all(len(v) >= 4 for v in rosters.values()):
            break  # most teams have a full board-1..4 roster by round 3; stop early once so
    return rosters


def compute_actual_final_standings(conn, tournament_id: str) -> list[int]:
    match_points: dict[int, int] = {}
    history: dict[int, list[RoundRecord]] = {}
    rounds = [
        r["round"]
        for r in conn.execute(
            "SELECT DISTINCT round FROM matches WHERE tournament_id = ? ORDER BY round", (tournament_id,)
        ).fetchall()
    ]
    num_rounds = len(rounds)
    for idx, rd in enumerate(rounds):
        remaining_after = num_rounds - idx - 1
        rows = conn.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND round = ?", (tournament_id, rd)
        ).fetchall()
        for r in rows:
            a, b = r["team_a_no"], r["team_b_no"]
            match_points.setdefault(a, 0)
            match_points.setdefault(b, 0)
            history.setdefault(a, [])
            history.setdefault(b, [])
            cmp_a, cmp_b = match_points[a], match_points[b]
            match_points[a] += r["team_a_match_pts"]
            match_points[b] += r["team_b_match_pts"]
            history[a].append(RoundRecord(opponent_no=b, game_points=r["team_a_game_pts"], cmp_before=cmp_a, remaining_rounds_after=remaining_after))
            history[b].append(RoundRecord(opponent_no=a, game_points=r["team_b_game_pts"], cmp_before=cmp_b, remaining_rounds_after=remaining_after))
    tiebreaks = compute_tiebreaks(history, match_points)
    return rank_teams(match_points, tiebreaks)


@dataclass
class BacktestResult:
    tournament_id: str
    brier_medal: float
    spearman_rank_corr: float
    n_teams: int


def _spearman(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 2:
        return float("nan")
    rx = _ranks(x)
    ry = _ranks(y)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - 6 * d2 / (n * (n**2 - 1))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for pos, idx in enumerate(order):
        ranks[idx] = pos + 1
    return ranks


def run_backtest(tournament_id: str, iterations: int = 500, progress=None) -> BacktestResult:
    from chessolympiad.data import db

    conn = db.connect()
    try:
        teams = load_teams(conn, tournament_id)
        rosters = build_rosters_from_round1(conn, tournament_id)
        teams = [t for t in teams if len(rosters.get(t.team_no, [])) >= 4]

        forecasts, _players = run_monte_carlo(teams, rosters, num_rounds=11, iterations=iterations, progress=progress)

        actual_ranking = compute_actual_final_standings(conn, tournament_id)
        actual_ranking = [t for t in actual_ranking if t in {tm.team_no for tm in teams}]
        actual_rank_pos = {t: i + 1 for i, t in enumerate(actual_ranking)}

        sq_errs = []
        sim_ranks, real_ranks = [], []
        for t in teams:
            if t.team_no not in actual_rank_pos:
                continue
            actual_medal = 1.0 if actual_rank_pos[t.team_no] <= 3 else 0.0
            pred = forecasts[t.team_no].p_any_medal
            sq_errs.append((pred - actual_medal) ** 2)
            sim_ranks.append(forecasts[t.team_no].expected_rank)
            real_ranks.append(actual_rank_pos[t.team_no])

        brier = sum(sq_errs) / len(sq_errs)
        corr = _spearman(sim_ranks, real_ranks)
        return BacktestResult(tournament_id=tournament_id, brier_medal=brier, spearman_rank_corr=corr, n_teams=len(sq_errs))
    finally:
        conn.close()


if __name__ == "__main__":
    for tid in ["2022-open", "2022-women", "2024-open", "2024-women"]:
        res = run_backtest(tid, iterations=500, progress=lambda m: print(m, flush=True) if m.endswith("0") else None)
        print(res, flush=True)
