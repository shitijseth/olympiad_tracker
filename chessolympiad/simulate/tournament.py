"""Monte Carlo tournament simulator: ties together pairing, match
simulation, tiebreaks, and Olympiad medal rules (Articles 4.9-4.10) into
per-team and per-player forecast probabilities.

Real rounds already played (live-update mode) are ingested via
simulate.round.apply_real_round instead of simulated, so only the
*remaining* rounds carry Monte Carlo uncertainty.
"""

from __future__ import annotations

import math
import os
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from chessolympiad.pairing.swiss_team import TeamPairingState, make_pairings
from chessolympiad.simulate.board_tiebreak import MIN_GAMES_FOR_BOARD_MEDAL
from chessolympiad.simulate.round import PlayerStat, TournamentState, apply_boundary_round, apply_pairings, apply_real_round
from chessolympiad.simulate.tiebreak import compute_tiebreaks, rank_teams


def _clone_state(state: TournamentState) -> TournamentState:
    """A fast, shallow-per-field clone -- not copy.deepcopy, which would
    recursively walk (and needlessly copy) `rosters`/`team_ratings` too,
    even though those are static config no iteration ever mutates. Every
    field that *is* mutated during simulation (pairing_states' opponents
    sets and counters, match_points, history, player_stats) gets its own
    fresh copy so iterations can't contaminate each other or the shared
    base_state this is cloned from.
    """
    return TournamentState(
        pairing_states={
            no: TeamPairingState(
                team_no=ps.team_no, seed=ps.seed, match_points=ps.match_points,
                opponents=set(ps.opponents), had_bye=ps.had_bye,
                white_odd_count=ps.white_odd_count, white_even_count=ps.white_even_count,
            )
            for no, ps in state.pairing_states.items()
        },
        rosters=state.rosters,
        team_ratings=state.team_ratings,
        match_points=dict(state.match_points),
        history={team_no: list(recs) for team_no, recs in state.history.items()},
        player_stats={
            key: PlayerStat(
                team_no=ps.team_no, board_no=ps.board_no, fide_id=ps.fide_id, name=ps.name,
                score=ps.score, games=ps.games, opp_rating_sum=ps.opp_rating_sum,
            )
            for key, ps in state.player_stats.items()
        },
    )


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


def _simulate_chunk(
    teams: list[TeamInfo],
    rosters: dict[int, list[dict]],
    num_rounds: int,
    iterations: int,
    real_rounds: dict[int, RealRoundData] | None,
    boundary_round: tuple[int, list[dict]] | None,
    seed: int | None,
    progress,
) -> dict:
    """Run `iterations` Monte Carlo iterations and return *raw* accumulators
    (sums/counts) -- not normalized TeamForecast/PlayerForecast objects.
    Normalizing (dividing by iteration count) has to happen exactly once,
    on the *combined* totals across every chunk that contributed to a
    result; doing it per-chunk first and averaging those would be wrong
    unless every chunk ran the same number of iterations (see
    run_monte_carlo_parallel, the only other caller of this).
    """
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

    # Real rounds carry no randomness -- replaying the leading contiguous
    # prefix of them (round 1, 2, 3, ... for as long as each is real) gives
    # the exact same resulting state on every single iteration. Doing that
    # once here instead of once per iteration is pure waste elimination,
    # not a precision change: iterations only ever diverge once real data
    # runs out, so this cached prefix is cloned (cheaply -- see
    # _clone_state) rather than recomputed from scratch each time. A real
    # round *after* a gap (round 3 real but round 2 isn't -- not expected
    # from how real_rounds is built, but not assumed away here either)
    # falls back to the old per-iteration path, since it depends on
    # synthetic state a cached prefix can't capture.
    real_round_prefix: list[int] = []
    rd_probe = 1
    while rd_probe in real_rounds:
        real_round_prefix.append(rd_probe)
        rd_probe += 1
    prefix_set = set(real_round_prefix)

    base_pairing_states = {t.team_no: TeamPairingState(team_no=t.team_no, seed=t.initial_rank) for t in teams}
    base_state = TournamentState(pairing_states=base_pairing_states, rosters=rosters, team_ratings=team_ratings)
    for rd in real_round_prefix:
        apply_real_round(base_state, real_rounds[rd].matches, real_rounds[rd].games, num_rounds - rd)

    for it in range(iterations):
        state = _clone_state(base_state)

        for rd in range(1, num_rounds + 1):
            if rd in prefix_set:
                continue  # already applied to base_state above
            remaining_after = num_rounds - rd
            if rd in real_rounds:
                apply_real_round(state, real_rounds[rd].matches, real_rounds[rd].games, remaining_after)
            elif boundary_round is not None and rd == boundary_round[0]:
                apply_boundary_round(state, boundary_round[1], remaining_after, rng)
            else:
                pairings = make_pairings(list(state.pairing_states.values()))
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
            if stat.games >= MIN_GAMES_FOR_BOARD_MEDAL:
                tpr = stat.opp_rating_sum / stat.games + 800.0 * (stat.score / stat.games - 0.5)
                player_tpr_sum[key] = player_tpr_sum.get(key, 0.0) + tpr
                player_tpr_n[key] = player_tpr_n.get(key, 0) + 1
                player_meta[key] = (stat.fide_id, stat.name)

        if progress and (it + 1) % max(1, iterations // 10) == 0:
            progress(f"simulation {it + 1}/{iterations}")

        # per-iteration board medals: top 3 TPR among eligible players
        # (Article 4.6.3.1's >=8-games rule), per board_no. TPR ties within
        # a single iteration are practically impossible (continuous
        # float ratings + a random per-match shock), so Appendix 2.III's
        # games-played tiebreak isn't applied here -- see
        # chessolympiad.simulate.board_tiebreak for where it matters: the
        # real (not simulated) standings, computed once, not per iteration.
        by_board: dict[int, list[tuple[float, tuple[int, int, str]]]] = {}
        for key, stat in state.player_stats.items():
            if stat.games >= MIN_GAMES_FOR_BOARD_MEDAL:
                tpr = stat.opp_rating_sum / stat.games + 800.0 * (stat.score / stat.games - 0.5)
                by_board.setdefault(stat.board_no, []).append((tpr, key))
        for board_no, entries in by_board.items():
            entries.sort(key=lambda e: -e[0])
            for tpr, key in entries[:3]:
                player_medal_counts[key] = player_medal_counts.get(key, 0) + 1

    return {
        "iterations": iterations,
        "rank_sum": rank_sum,
        "rank_sumsq": rank_sumsq,
        "mp_sum": mp_sum,
        "medal_counts": medal_counts,
        "top8_counts": top8_counts,
        "top16_counts": top16_counts,
        "category_medal_counts": category_medal_counts,
        "player_medal_counts": player_medal_counts,
        "player_tpr_sum": player_tpr_sum,
        "player_tpr_n": player_tpr_n,
        "player_meta": player_meta,
    }


def _merge_chunks(chunks: list[dict], teams: list[TeamInfo]) -> dict:
    """Sum raw accumulators from independent chunks (each from its own
    seeded _simulate_chunk call) into one combined set, as if a single
    process had run all of them back to back. Addition is associative, so
    this is exact -- see the seeded-equality test that pins this down.
    """
    merged = {
        "iterations": sum(c["iterations"] for c in chunks),
        "rank_sum": {t.team_no: 0.0 for t in teams},
        "rank_sumsq": {t.team_no: 0.0 for t in teams},
        "mp_sum": {t.team_no: 0.0 for t in teams},
        "medal_counts": {t.team_no: {"gold": 0, "silver": 0, "bronze": 0} for t in teams},
        "top8_counts": {t.team_no: 0 for t in teams},
        "top16_counts": {t.team_no: 0 for t in teams},
        "category_medal_counts": {t.team_no: 0 for t in teams},
        "player_medal_counts": {},
        "player_tpr_sum": {},
        "player_tpr_n": {},
        "player_meta": {},
    }
    for c in chunks:
        for tn in merged["rank_sum"]:
            merged["rank_sum"][tn] += c["rank_sum"][tn]
            merged["rank_sumsq"][tn] += c["rank_sumsq"][tn]
            merged["mp_sum"][tn] += c["mp_sum"][tn]
            merged["top8_counts"][tn] += c["top8_counts"][tn]
            merged["top16_counts"][tn] += c["top16_counts"][tn]
            merged["category_medal_counts"][tn] += c["category_medal_counts"][tn]
            for medal in ("gold", "silver", "bronze"):
                merged["medal_counts"][tn][medal] += c["medal_counts"][tn][medal]
        for key, n in c["player_medal_counts"].items():
            merged["player_medal_counts"][key] = merged["player_medal_counts"].get(key, 0) + n
        for key, s in c["player_tpr_sum"].items():
            merged["player_tpr_sum"][key] = merged["player_tpr_sum"].get(key, 0.0) + s
            merged["player_tpr_n"][key] = merged["player_tpr_n"].get(key, 0) + c["player_tpr_n"][key]
        merged["player_meta"].update(c["player_meta"])
    return merged


def _normalize(
    acc: dict, teams: list[TeamInfo]
) -> tuple[dict[int, TeamForecast], dict[tuple[int, int, str], PlayerForecast]]:
    iterations = acc["iterations"]
    rank_sum, rank_sumsq, mp_sum = acc["rank_sum"], acc["rank_sumsq"], acc["mp_sum"]
    medal_counts, top8_counts, top16_counts = acc["medal_counts"], acc["top8_counts"], acc["top16_counts"]
    category_medal_counts = acc["category_medal_counts"]
    player_medal_counts, player_tpr_sum = acc["player_medal_counts"], acc["player_tpr_sum"]
    player_tpr_n, player_meta = acc["player_tpr_n"], acc["player_meta"]

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
    chunk = _simulate_chunk(teams, rosters, num_rounds, iterations, real_rounds, boundary_round, seed, progress)
    return _normalize(chunk, teams)


def run_monte_carlo_parallel(
    teams: list[TeamInfo],
    rosters: dict[int, list[dict]],
    num_rounds: int,
    iterations: int,
    real_rounds: dict[int, RealRoundData] | None = None,
    boundary_round: tuple[int, list[dict]] | None = None,
    seed: int | None = None,
    max_workers: int = 4,
    progress=None,
) -> tuple[dict[int, TeamForecast], dict[tuple[int, int, str], PlayerForecast]]:
    """Same result distribution as run_monte_carlo (each worker is just an
    independently-seeded run of the identical _simulate_chunk code, and the
    combination is an exact sum, not an approximation -- see
    tests/test_parallel_monte_carlo.py), spread across multiple processes.

    max_workers defaults to a deliberately conservative 4, and is further
    capped below (cpu_count - 1): this machine also runs an unattended
    cron job every 2 minutes for the live 2026 event, and the point of
    parallelizing at all is to finish faster, not to compete with everything
    else running on the machine for every core it has. Pass a higher
    max_workers explicitly if you want more throughput and accept more
    resource contention while it runs.
    """
    cpu_budget = max(1, (os.cpu_count() or 1) - 1)
    workers = max(1, min(max_workers, cpu_budget, iterations))

    base = iterations // workers
    remainder = iterations % workers
    chunk_sizes = [base + (1 if i < remainder else 0) for i in range(workers)]
    chunk_sizes = [n for n in chunk_sizes if n > 0]

    # Independent seeds per worker, derived from the caller's seed so the
    # whole run is still reproducible end to end, but no two workers ever
    # sample the same random stream.
    seed_rng = random.Random(seed)
    seeds = [seed_rng.randrange(2**31) for _ in chunk_sizes]

    # Submitted concurrently (all workers run at once regardless of the
    # order below), but *collected* in a fixed order -- as_completed()
    # would hand results back in whatever order each process happens to
    # finish, which varies run to run with OS scheduling, and merging
    # floats in a different order each time produces genuinely different
    # (if utterly negligible) sums, since float addition isn't associative.
    # Fixed collection order makes the result exactly reproducible for a
    # given seed regardless of timing -- see the aggregation-determinism
    # test this was built to satisfy.
    with ProcessPoolExecutor(max_workers=len(chunk_sizes)) as ex:
        futures = [
            ex.submit(_simulate_chunk, teams, rosters, num_rounds, n, real_rounds, boundary_round, s, None)
            for n, s in zip(chunk_sizes, seeds)
        ]
        chunks = []
        for i, fut in enumerate(futures):
            chunks.append(fut.result())
            if progress:
                progress(f"simulation: {i + 1}/{len(futures)} worker chunks done ({sum(c['iterations'] for c in chunks)}/{iterations} iterations)")

    merged = _merge_chunks(chunks, teams)
    return _normalize(merged, teams)
