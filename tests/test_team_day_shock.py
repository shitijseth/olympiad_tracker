import random

import numpy as np

from chessolympiad.model import elo
from chessolympiad.simulate.match import simulate_match


def test_team_day_shock_reproduces_measured_board_correlation():
    """Regression guard for the calibrated TEAM_DAY_SHOCK_SD constant: board
    results within a match should correlate at roughly the level measured
    on real 2022/2024 data (avg +0.072 across 2,811 matches), not be
    independent (r=0) like a naive per-board simulation would produce.
    """
    rng = random.Random(123)
    lineup_a = [{"fide_id": i, "name": f"A{i}", "rating": r} for i, r in enumerate([2500, 2450, 2400, 2350])]
    lineup_b = [{"fide_id": i, "name": f"B{i}", "rating": r} for i, r in enumerate([2460, 2410, 2360, 2310])]

    residuals = [[] for _ in range(4)]
    for _ in range(6000):
        _a_pts, _b_pts, boards = simulate_match(lineup_a, lineup_b, True, rng)
        for i, b in enumerate(boards):
            p_white, p_draw, _ = elo.outcome_probs(b["white_rating"], b["black_rating"])
            expected_a = (p_white + 0.5 * p_draw) if b["white_is_team_a"] else (1 - p_white - 0.5 * p_draw)
            actual_a = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[b["result"]]
            if not b["white_is_team_a"]:
                actual_a = 1 - actual_a
            residuals[i].append(actual_a - expected_a)

    corr = np.corrcoef(np.array(residuals))
    pairs = [(i, j) for i in range(4) for j in range(i + 1, 4)]
    avg_corr = float(np.mean([corr[i, j] for i, j in pairs]))
    assert 0.03 < avg_corr < 0.13, f"expected board-residual correlation near 0.07, got {avg_corr:.3f}"
