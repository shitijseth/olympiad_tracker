"""Which 4 of a team's 5 nominated players take the board each round.

`active_lineup` is the naive top-4-every-round baseline, kept for callers
that don't have opponent context (e.g. board-medal bookkeeping). For actual
match simulation, `choose_lineup` models real captain behavior: rest a
board and field the reserve, more often against weaker opponents -- see
model/constants.py for the fitted probabilities and the empirical study
behind them (chessolympiad/analysis, lineup-rotation test).
"""

from __future__ import annotations

import math

from chessolympiad.model import constants as C


def active_lineup(roster: list[dict]) -> list[dict]:
    """roster: list of {board_no, fide_id, name, rating, ...} for one team
    (up to 5 entries). Returns the 4 who play, ordered by board_no 1-4,
    ignoring any reserve rotation.
    """
    boards_1_to_4 = [p for p in roster if p["board_no"] <= 4]
    boards_1_to_4.sort(key=lambda p: p["board_no"])
    return boards_1_to_4


def choose_lineup(roster: list[dict], own_rating_avg: float, opp_rating_avg: float, rng) -> list[dict]:
    """The 4 who play this specific match, allowing for at most one
    reserve-for-a-board substitution (only one reserve exists). Boards are
    checked 4, 3, 2, 1 in order -- matching the real tendency to rest the
    lower boards first -- and the first one to trigger gets the reserve.
    """
    lineup = active_lineup(roster)
    reserve = next((p for p in roster if p["board_no"] == 5), None)
    if reserve is None:
        return lineup

    gap = (own_rating_avg - opp_rating_avg) / 100.0
    for board_no in (4, 3, 2, 1):
        z = C.LINEUP_SUB_INTERCEPTS[board_no - 1] + C.LINEUP_SUB_GAP_SLOPE * gap
        p_sub = 1.0 / (1.0 + math.exp(-z))
        if rng.random() < p_sub:
            return [reserve if p["board_no"] == board_no else p for p in lineup]
    return lineup
