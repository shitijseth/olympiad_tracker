"""Simulate one team match: 4 boards, colors alternating per the standard
team-chess rule (one team gets white on boards 1&3, the other on 2&4 --
confirmed against real Olympiad data).
"""

from __future__ import annotations

from chessolympiad.model import constants as C
from chessolympiad.model import elo


def simulate_match(
    team_a_lineup: list[dict],
    team_b_lineup: list[dict],
    a_white_odd_boards: bool,
    rng,
) -> tuple[float, float, list[dict]]:
    """lineup entries: {fide_id, name, rating}, board order 1-4 (index 0..3).
    Returns (team_a_game_points, team_b_game_points, board_results) where
    board_results[i] = {board_no, white_fide_id, white_name, white_rating,
    black_fide_id, black_name, black_rating, result}.

    A shared per-match "team day" shock is added to each team's players
    before sampling results (not stored -- reported ratings stay the
    player's real rating). This reproduces the measured tendency for a
    team's 4 boards to move together more than 4 independent games would
    (see chessolympiad/analysis/factor_study.py and model/constants.py).
    """
    shock_a = rng.gauss(0, C.TEAM_DAY_SHOCK_SD)
    shock_b = rng.gauss(0, C.TEAM_DAY_SHOCK_SD)

    a_pts = b_pts = 0.0
    boards = []
    for i in range(4):
        board_no = i + 1
        a_player, b_player = team_a_lineup[i], team_b_lineup[i]
        a_is_white = a_white_odd_boards if board_no % 2 == 1 else not a_white_odd_boards
        white, black = (a_player, b_player) if a_is_white else (b_player, a_player)
        white_shock, black_shock = (shock_a, shock_b) if (white is a_player) else (shock_b, shock_a)

        result = elo.sample_result(white["rating"] + white_shock, black["rating"] + black_shock, rng)
        w_score, b_score = elo.result_to_score(result)
        if a_is_white:
            a_pts += w_score
            b_pts += b_score
        else:
            a_pts += b_score
            b_pts += w_score

        boards.append(
            {
                "board_no": board_no,
                "white_is_team_a": a_is_white,
                "white_fide_id": white.get("fide_id"),
                "white_name": white.get("name"),
                "white_rating": white["rating"],
                "black_fide_id": black.get("fide_id"),
                "black_name": black.get("name"),
                "black_rating": black["rating"],
                "result": result,
            }
        )
    return a_pts, b_pts, boards


def match_points_from_game_points(a_pts: float, b_pts: float) -> tuple[int, int]:
    """Article 4.9.1: win the match (>2 of 4 game points) = 2 MP, draw = 1-1, loss = 0."""
    if a_pts > b_pts:
        return 2, 0
    if a_pts < b_pts:
        return 0, 2
    return 1, 1
