"""Monte Carlo tournament simulator: ties together pairing, match
simulation, tiebreaks, and Olympiad medal rules (Articles 4.9-4.10) into
per-team and per-player forecast probabilities.

Real rounds already played (live-update mode) are ingested via
simulate.round.apply_real_round instead of simulated, so only the
*remaining* rounds carry Monte Carlo uncertainty.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from chessolympiad.pairing.swiss_team import TeamPairingState, make_pairings
from chessolympiad.simulate.round import TournamentState, apply_boundary_round, apply_pairings, apply_real_round
from chessolympiad.simulate.tiebreak import compute_tiebreaks, rank_teams


@dataclass
class TeamInfo:
    team_no: int
    federation: str
    team_name: str
    initial_rank: int
    rating_avg: float = 0.0


@dataclass
class RealRoundData:
    matches: list[dict]
    games: list[dict]


@dataclass
class TeamForecast:
    team_no: int
    p_gold: float = 0.0
    p_silver: float = 0.0
    p_bronze: float = 0.0
    p_any_medal: float = 0.0
    p_top8: float = 0.0
    p_top16: float = 0.0
    p_category_medal: float = 0.0  # Article 4.10.2 rating-category gold/silver/bronze, any color
    expected_rank: float = 0.0
    expected_match_pts: float = 0.0
    rank_std: float = 0.0


@dataclass
class PlayerForecast:
    team_no: int
    board_no: int
    player_key: str
    fide_id: int | None
    name: str
    p_board_medal: float = 0.0
    expected_tpr: float = 0.0


def _rating_category_medals(
    teams: list[TeamInfo], ranking: list[int], overall_medalists: set[int]
) -> dict[int, str]:
    """Article 4.10.2: partition teams into 5 equal-ish groups by initial
    rank; within each group, the best 3 finishers (by overall standing) who
    haven't already won an overall medal get gold/silver/bronze for that
    category.
    """
    ordered_by_seed = sorted(teams, key=lambda t: t.initial_rank)
    n = len(ordered_by_seed)
    group_size = -(-n // 5)  # ceil
    categories = [ordered_by_seed[i : i + group_size] for i in range(0, n, group_size)]
    rank_pos = {team_no: i for i, team_no in enumerate(ranking)}

    result: dict[int, str] = {}
    for cat in categories:
        eligible = [t.team_no for t in cat if t.team_no not in overall_medalists]
        eligible.sort(key=lambda tn: rank_pos.get(tn, 10**9))
        medals = ["cat_gold", "cat_silver", "cat_bronze"]
        for tn, medal in zip(eligible, medals):
            result[tn] = medal
    return result


def run_monte_carlo(
    teams: list[TeamInfo],
    rosters: dict[int, list[dict]],
    num_rounds: int,
    iterations: int,
    real_rounds: dict[int, RealRoundData] | None = None,
    boundary_round: tuple[int, list[dict]] | None = None,
    seed: int | None = None,
    progress=None,
) -> tuple[dict[int, TeamForecast], dict[tuple[int, int, str], PlayerForecast]]:
    real_rounds = real_rounds or {}
    rng = random.Random(seed)

    # Running sum/sum-of-squares accumulators, NOT per-iteration lists: at
    # 50,000+ iterations, storing every iteration's rank/match-points/TPR
    # for every team grows unbounded when all we ever need from them is
    # mean and std dev, which these accumulators give in O(1) space per
    # team regardless of iteration count.
    rank_sum: dict[int, float] = {t.team_no: 0.0 for t in teams}
    rank_sumsq: dict[int, float] = {t.team_no: 0.0 for t in teams}
    mp_sum: dict[int, float] = {t.team_no: 0.0 for t in teams}
    medal_counts: dict[int, dict[str, int]] = {t.team_no: {"gold": 0, "silver": 0, "bronze": 0} for t in teams}
    top8_counts: dict[int, int] = {t.team_no: 0 for t in teams}
    top16_counts: dict[int, int] = {t.team_no: 0 for t in teams}
    category_medal_counts: dict[int, int] = {t.team_no: 0 for t in teams}

    player_medal_counts: dict[tuple[int, int, str], int] = {}
    player_tpr_sum: dict[tuple[int, int, str], float] = {}
    player_tpr_n: dict[tuple[int, int, str], int] = {}
    player_meta: dict[tuple[int, int, str], tuple[int | None, str]] = {}

    team_ratings = {t.team_no: t.rating_avg for t in teams}

    for it in range(iterations):
        pairing_states = {t.team_no: TeamPairingState(team_no=t.team_no, seed=t.initial_rank) for t in teams}
        state = TournamentState(pairing_states=pairing_states, rosters=rosters, team_ratings=team_ratings)

        for rd in range(1, num_rounds + 1):
            remaining_after = num_rounds - rd
            if rd in real_rounds:
                apply_real_round(state, real_rounds[rd].matches, real_rounds[rd].games, remaining_after)
            elif boundary_round is not None and rd == boundary_round[0]:
                apply_boundary_round(state, boundary_round[1], remaining_after, rng)
            else:
                pairings = make_pairings(list(pairing_states.values()))
                apply_pairings(state, pairings, rng, remaining_after)

        tiebreaks = compute_tiebreaks(state.history, state.match_points)
        ranking = rank_teams(state.match_points, tiebreaks)
        overall_medalists = set(ranking[:3])

        for pos, team_no in enumerate(ranking):
            rank_sum[team_no] += pos + 1
            rank_sumsq[team_no] += (pos + 1) ** 2
            mp_sum[team_no] += state.match_points[team_no]
            if pos < 8:
                top8_counts[team_no] += 1
            if pos < 16:
                top16_counts[team_no] += 1
        if len(ranking) > 0:
            medal_counts[ranking[0]]["gold"] += 1
        if len(ranking) > 1:
            medal_counts[ranking[1]]["silver"] += 1
        if len(ranking) > 2:
            medal_counts[ranking[2]]["bronze"] += 1

        for tn in _rating_category_medals(teams, ranking, overall_medalists):
            category_medal_counts[tn] += 1

        for key, stat in state.player_stats.items():
            if stat.games >= 8:
                tpr = stat.opp_rating_sum / stat.games + 800.0 * (stat.score / stat.games - 0.5)
                player_tpr_sum[key] = player_tpr_sum.get(key, 0.0) + tpr
                player_tpr_n[key] = player_tpr_n.get(key, 0) + 1
                player_meta[key] = (stat.fide_id, stat.name)

        if progress and (it + 1) % max(1, iterations // 10) == 0:
            progress(f"simulation {it + 1}/{iterations}")

        # per-iteration board medals (top 3 TPR among players with >=8 games, per board_no)
        by_board: dict[int, list[tuple[float, tuple[int, int, str]]]] = {}
        for key, stat in state.player_stats.items():
            if stat.games >= 8:
                tpr = stat.opp_rating_sum / stat.games + 800.0 * (stat.score / stat.games - 0.5)
                by_board.setdefault(stat.board_no, []).append((tpr, key))
        for board_no, entries in by_board.items():
            entries.sort(key=lambda e: -e[0])
            for tpr, key in entries[:3]:
                player_medal_counts[key] = player_medal_counts.get(key, 0) + 1

    team_forecasts: dict[int, TeamForecast] = {}
    for t in teams:
        mc = medal_counts[t.team_no]
        mean_rank = rank_sum[t.team_no] / iterations
        variance = max(0.0, rank_sumsq[t.team_no] / iterations - mean_rank**2)
        team_forecasts[t.team_no] = TeamForecast(
            team_no=t.team_no,
            p_gold=mc["gold"] / iterations,
            p_silver=mc["silver"] / iterations,
            p_bronze=mc["bronze"] / iterations,
            p_any_medal=(mc["gold"] + mc["silver"] + mc["bronze"]) / iterations,
            p_top8=top8_counts[t.team_no] / iterations,
            p_top16=top16_counts[t.team_no] / iterations,
            p_category_medal=category_medal_counts[t.team_no] / iterations,
            expected_rank=mean_rank,
            expected_match_pts=mp_sum[t.team_no] / iterations,
            rank_std=math.sqrt(variance),
        )

    player_forecasts: dict[tuple[int, int, str], PlayerForecast] = {}
    for key, tpr_sum in player_tpr_sum.items():
        fide_id, name = player_meta[key]
        player_forecasts[key] = PlayerForecast(
            team_no=key[0],
            board_no=key[1],
            player_key=key[2],
            fide_id=fide_id,
            name=name,
            p_board_medal=player_medal_counts.get(key, 0) / iterations,
            expected_tpr=tpr_sum / player_tpr_n[key],
        )

    return team_forecasts, player_forecasts
