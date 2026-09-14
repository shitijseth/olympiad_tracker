"""Per-game outcome probabilities: the Davidson model (Elo-with-draws).

For white rating Rw and black rating Rb:
    a = 10^(Rw/400), b = 10^(Rb/400), d = nu * 10^((Rw+Rb)/800)
    P(white win)  = a / (a + b + d)
    P(black win)  = b / (a + b + d)
    P(draw)       = d / (a + b + d)

nu controls how draw-prone the game is; it is *not* a single constant here --
it's fit as a function of the average rating of the two players, because
grandmaster games draw far more often than club-level games at the same
rating gap (see model.constants and model.calibration).
"""

from __future__ import annotations

import math

from chessolympiad.model import constants as C


def nu_for_avg_rating(avg_rating: float, c0: float = C.NU_C0, c1: float = C.NU_C1) -> float:
    return math.exp(c0 + c1 * avg_rating / 400.0)


def outcome_probs(
    rating_white: float,
    rating_black: float,
    nu: float | None = None,
    white_adv: float = C.WHITE_ADV,
) -> tuple[float, float, float]:
    """Returns (P(white win), P(draw), P(black win))."""
    rw = rating_white + white_adv
    rb = rating_black
    if nu is None:
        nu = nu_for_avg_rating((rating_white + rating_black) / 2.0)

    a = 10 ** (rw / 400.0)
    b = 10 ** (rb / 400.0)
    d = nu * 10 ** ((rw + rb) / 800.0)
    denom = a + b + d
    return a / denom, d / denom, b / denom


def sample_result(rating_white: float, rating_black: float, rng, nu: float | None = None) -> str:
    """Draw one game outcome. Returns '1-0', '1/2-1/2', or '0-1'."""
    p_white, p_draw, _p_black = outcome_probs(rating_white, rating_black, nu)
    u = rng.random()
    if u < p_white:
        return "1-0"
    if u < p_white + p_draw:
        return "1/2-1/2"
    return "0-1"


def result_to_score(result: str) -> tuple[float, float]:
    """(white_score, black_score) for a result string."""
    return {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}[result]
