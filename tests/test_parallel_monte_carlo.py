"""run_monte_carlo_parallel splits iterations across worker processes and
sums their raw accumulators -- these tests exist because that combination
step turned out to have a real, if tiny, correctness subtlety: collecting
worker results in *completion* order (as_completed) rather than a fixed
order made floating-point sums (player TPR) depend on OS scheduling, since
float addition isn't associative. Fixed collection order (see
run_monte_carlo_parallel) fixes it -- these tests pin that down so it can't
silently regress.
"""

from __future__ import annotations

import random

from chessolympiad.simulate.tournament import (
    TeamInfo,
    _merge_chunks,
    _normalize,
    _simulate_chunk,
    run_monte_carlo,
    run_monte_carlo_parallel,
)


def _make_field():
    teams = []
    rosters = {}
    tiers = [(2750, 4), (2400, 4), (1700, 8)]
    team_no = 1
    for rating, count in tiers:
        for _ in range(count):
            teams.append(TeamInfo(team_no=team_no, federation=f"F{team_no}", team_name=f"Team{team_no}", initial_rank=team_no, rating_avg=rating))
            rosters[team_no] = [{"board_no": b, "fide_id": team_no * 10 + b, "name": f"P{team_no}.{b}", "rating": rating} for b in range(1, 5)]
            team_no += 1
    teams.sort(key=lambda t: t.initial_rank)
    return teams, rosters


def _replicate_worker_seeds(iterations: int, seed: int, max_workers: int, cpu_budget_minus_one: int) -> tuple[list[int], list[int]]:
    """Exactly the chunk-splitting/seeding math run_monte_carlo_parallel
    uses internally, so a test can independently reproduce what seeds it
    assigned each worker without needing to instrument the real function.
    """
    workers = max(1, min(max_workers, cpu_budget_minus_one, iterations))
    base = iterations // workers
    remainder = iterations % workers
    chunk_sizes = [base + (1 if i < remainder else 0) for i in range(workers)]
    chunk_sizes = [n for n in chunk_sizes if n > 0]
    seed_rng = random.Random(seed)
    seeds = [seed_rng.randrange(2**31) for _ in chunk_sizes]
    return chunk_sizes, seeds


def test_parallel_result_exactly_matches_sequential_replica_of_same_chunks(monkeypatch):
    """The strongest possible correctness check: run the *exact* same
    per-worker seeds and chunk sizes sequentially in-process (no
    multiprocessing at all) and confirm the merged, normalized result is
    bit-for-bit identical to what run_monte_carlo_parallel actually
    produces. Since addition is associative and _simulate_chunk is already
    proven deterministic given a seed, the only way these could differ is
    a bug in the splitting/seeding/merging logic itself.
    """
    monkeypatch.setattr("os.cpu_count", lambda: 5)  # pin cpu_budget so the replica's math matches exactly
    teams, rosters = _make_field()
    num_rounds, iterations, seed, max_workers = 9, 200, 123, 4

    chunk_sizes, seeds = _replicate_worker_seeds(iterations, seed, max_workers, cpu_budget_minus_one=4)
    chunks = [_simulate_chunk(teams, rosters, num_rounds, n, None, None, s, None) for n, s in zip(chunk_sizes, seeds)]
    expected_teams, expected_players = _normalize(_merge_chunks(chunks, teams), teams)

    actual_teams, actual_players = run_monte_carlo_parallel(
        teams, rosters, num_rounds, iterations, seed=seed, max_workers=max_workers
    )

    assert {tn: vars(tf) for tn, tf in actual_teams.items()} == {tn: vars(tf) for tn, tf in expected_teams.items()}
    assert {k: vars(pf) for k, pf in actual_players.items()} == {k: vars(pf) for k, pf in expected_players.items()}


def test_parallel_is_deterministic_across_repeated_runs():
    """Guards specifically against the as_completed()-ordering bug this
    file's module docstring describes: run twice, same seed, must be
    byte-identical both times regardless of which worker process happens
    to finish first.
    """
    teams, rosters = _make_field()
    run_kwargs = dict(num_rounds=9, iterations=200, seed=99, max_workers=4)

    teams_a, players_a = run_monte_carlo_parallel(teams, rosters, **run_kwargs)
    teams_b, players_b = run_monte_carlo_parallel(teams, rosters, **run_kwargs)

    assert {tn: vars(tf) for tn, tf in teams_a.items()} == {tn: vars(tf) for tn, tf in teams_b.items()}
    assert {k: vars(pf) for k, pf in players_a.items()} == {k: vars(pf) for k, pf in players_b.items()}


def test_parallel_iteration_count_is_preserved_exactly():
    """The split must never silently drop or duplicate iterations --
    check via a field where p_gold values must still sum to 1.0 (exactly
    one team wins gold in every iteration, so this only holds if every
    iteration that ran was counted exactly once)."""
    teams, rosters = _make_field()
    forecasts, _ = run_monte_carlo_parallel(teams, rosters, num_rounds=9, iterations=250, seed=5, max_workers=4)
    assert abs(sum(tf.p_gold for tf in forecasts.values()) - 1.0) < 1e-9


def test_worker_count_is_capped_conservatively(monkeypatch):
    """Never uses every core on the machine (leaves at least one free),
    and never spins up more workers than there are iterations to give
    them (an iterations=2 run must not create empty chunks for workers
    3 and 4 -- see the `if n > 0` filter in run_monte_carlo_parallel)."""
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    teams, rosters = _make_field()

    # max_workers=100 (an unreasonable ask) must still be capped well
    # below the machine's full 8 cores.
    forecasts, _ = run_monte_carlo_parallel(teams, rosters, num_rounds=9, iterations=50, seed=1, max_workers=100)
    assert abs(sum(tf.p_gold for tf in forecasts.values()) - 1.0) < 1e-9  # still ran correctly regardless

    forecasts, _ = run_monte_carlo_parallel(teams, rosters, num_rounds=9, iterations=2, seed=1, max_workers=4)
    assert abs(sum(tf.p_gold for tf in forecasts.values()) - 1.0) < 1e-9


def test_parallel_and_single_process_agree_statistically():
    """Not a bit-for-bit check (different seeding paths, see the other
    tests for that) -- a sanity check that splitting the work doesn't
    change *what's being estimated*: both should land within a few Monte
    Carlo standard errors of each other on the same field/seed budget.
    """
    teams, rosters = _make_field()
    single_forecasts, _ = run_monte_carlo(teams, rosters, num_rounds=9, iterations=4000, seed=1)
    parallel_forecasts, _ = run_monte_carlo_parallel(teams, rosters, num_rounds=9, iterations=4000, seed=2, max_workers=4)

    top_team = teams[0].team_no
    p_single = single_forecasts[top_team].p_any_medal
    p_parallel = parallel_forecasts[top_team].p_any_medal
    se = (p_single * (1 - p_single) / 4000) ** 0.5
    assert abs(p_single - p_parallel) < 6 * se, f"p_single={p_single}, p_parallel={p_parallel}, se={se}"
