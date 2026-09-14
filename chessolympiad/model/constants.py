"""Fitted model constants. Values below were fit on real Olympiad game data
(see chessolympiad/model/calibration.py and backtest/run_backtest.py) --
they are the trusted, checked-in output of that fitting process, not
re-fit at runtime.

Draw propensity nu(avg_rating) = exp(NU_C0 + NU_C1 * avg_rating / 400)
-- higher average rating -> more draws, per the well-known empirical pattern
that grandmaster games draw far more often than club-level games at the same
rating gap. WHITE_ADV is added to White's rating before computing outcome
probabilities (first-move advantage).
"""

NU_C0: float = -3.250392
NU_C1: float = 0.570763
WHITE_ADV: float = 39.836484

# Below this many real games, the adaptive in-event rating (model.adaptive_rating)
# leans almost entirely on the pre-event FIDE rating rather than in-event form.
# Empirically validated: real 2022/2024 first-half vs second-half performance
# correlation (+0.14 to +0.23) implies an optimal shrinkage constant of ~20
# games via n/(n+K)=r with n~4-5 games/half -- matches this pre-existing value.
ADAPTIVE_RATING_FULL_WEIGHT_GAMES: int = 20

# Per-match, per-team "team day" shock: a shared random rating perturbation
# applied to all 4 of a team's boards for one match (redrawn every match),
# modelling the real, measured tendency for a team's boards to move together
# (blowouts/whitewashes happen more than 4 independent games would produce).
# Calibrated so the resulting board-residual correlation matches the ~0.072
# average pairwise correlation measured across 2,811 real Olympiad matches
# (chessolympiad/analysis/factor_study.py, test 2).
TEAM_DAY_SHOCK_SD: float = 87.0

# Lineup rotation: captains rest a board and field the reserve far more than
# a naive "static top-4" model assumes -- measured substitution rate per
# board across 2022+2024 was 17%/29%/37%/34% for boards 1-4, and rounds with
# a substitution had a rating gap (own avg - opponent avg) 18-32 points more
# favorable than rounds without, confirming captains rest players against
# weaker opponents. Fit as logistic P(substitution at board b) = sigmoid(
# LINEUP_SUB_INTERCEPTS[b-1] + LINEUP_SUB_GAP_SLOPE * (own_rtg - opp_rtg)/100)
# on ~30,000 real per-board-per-round observations (chessolympiad/analysis
# lineup-rotation study). At most one substitution per match is modelled
# (only one reserve exists); boards are checked 4,3,2,1 in that order and
# the first one to trigger gets the reserve.
LINEUP_SUB_INTERCEPTS: tuple[float, float, float, float] = (-1.5802, -0.8865, -0.5517, -0.6756)
LINEUP_SUB_GAP_SLOPE: float = 0.0249
