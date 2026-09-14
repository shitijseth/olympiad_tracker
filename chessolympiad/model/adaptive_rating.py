"""In-event adaptive rating: blend a player's pre-event FIDE rating with
their live tournament performance rating so far, for simulating the
remaining rounds once the real event is underway.

Without this, the live-update mode would just re-pair with static
pre-event ratings for all 11 rounds -- this is what makes each day's
re-simulation actually use newly-synced in-event game data.
"""

from __future__ import annotations

from chessolympiad.model import constants as C


def performance_rating(avg_opponent_rating: float, score: float, games: int) -> float:
    """Simplified linear FIDE performance-rating approximation:
    Rp = avg_opponent_rating + 800 * (score/games - 0.5).
    """
    if games == 0:
        return avg_opponent_rating
    return avg_opponent_rating + 800.0 * (score / games - 0.5)


def effective_rating(
    pre_event_rating: float,
    avg_opponent_rating: float | None,
    score: float,
    games: int,
    k: int = C.ADAPTIVE_RATING_FULL_WEIGHT_GAMES,
) -> float:
    """Shrinkage-weighted blend of pre-event rating and in-event performance.
    Weight on the live signal grows with games played: w = games / (games + k).
    A handful of games (typical mid-Olympiad) is noisy, so k defaults large
    enough that early rounds barely move a player's effective rating.
    """
    if games == 0 or avg_opponent_rating is None:
        return pre_event_rating
    rp = performance_rating(avg_opponent_rating, score, games)
    w = games / (games + k)
    return (1 - w) * pre_event_rating + w * rp
