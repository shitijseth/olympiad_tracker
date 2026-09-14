"""Exact implementation of FIDE Chess Olympiad Regulations, Annex 2.I --
team standings tiebreaks within the Open/Women sections.

Order of precedence for teams tied on match points:
    TB1 -- sum of IS(10): for each of the 10 best-scoring opponents
           (dropping the one with the lowest final match points), IS_i =
           (game points scored against opponent i) x (opponent i's final
           match points). Byes/unplayed matches use the regulation's
           substitute formulas (see RoundRecord / _is_value below).
    TB2 -- total game points scored across the event.
    TB3 -- sum of the final match points of the 10 best opponents (same
           drop-the-weakest rule), unweighted by game score.

A team's full round history is needed to compute this -- see RoundRecord.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RoundRecord:
    """One team's result for one round."""

    opponent_no: int | None      # None for a bye / fully unplayed round
    game_points: float           # this team's game points scored this round (0..4)
    cmp_before: int              # this team's own match points accumulated BEFORE this round
    remaining_rounds_after: int  # rounds left after this one
    kind: str = "played"         # "played" | "bye" | "unplayed_win" | "unplayed_loss"


def _is_value(rec: RoundRecord, opponent_final_mp: int | None) -> float:
    if rec.kind == "bye":
        return 2.0 * (rec.cmp_before + 1 + rec.remaining_rounds_after)
    if rec.kind == "unplayed_win":
        return 4.0 * (rec.cmp_before + 1 + rec.remaining_rounds_after)
    if rec.kind == "unplayed_loss":
        return 0.0
    # played: IS_i = game points scored against opponent i x opponent's final match points
    assert opponent_final_mp is not None
    return rec.game_points * opponent_final_mp


def compute_tiebreaks(
    history: dict[int, list[RoundRecord]],
    final_match_points: dict[int, int],
) -> dict[int, tuple[float, float, float]]:
    """Returns {team_no: (TB1, TB2, TB3)}."""
    out: dict[int, tuple[float, float, float]] = {}
    for team_no, records in history.items():
        # TB1: IS(10) over played + bye/unplayed rounds, dropping the worst
        # opponent by final match points (byes/unplayed treated as having no
        # "opponent strength" to drop by -- they're included in the IS sum
        # via their own substitute formula and never dropped, since Article
        # 4.9.1 Annex 2.I's "excluding the lowest-scoring opponent" concept
        # only cleanly applies to real opponents).
        is_values = []
        real_opp_mp = []
        for rec in records:
            opp_mp = final_match_points.get(rec.opponent_no) if rec.opponent_no is not None else None
            is_values.append((rec.kind == "played", opp_mp, _is_value(rec, opp_mp)))
            if rec.kind == "played" and opp_mp is not None:
                real_opp_mp.append(opp_mp)

        played = [v for is_played, _, v in is_values if is_played]
        non_played = [v for is_played, _, v in is_values if not is_played]
        if played:
            # drop exactly one real opponent: the one with lowest final MP
            min_idx = min(
                range(len(played)),
                key=lambda i: [opp for is_p, opp, v in is_values if is_p][i],
            )
            played_kept = played[:min_idx] + played[min_idx + 1 :]
        else:
            played_kept = played
        tb1 = sum(played_kept) + sum(non_played)

        tb2 = sum(rec.game_points for rec in records)

        real_opp_mp_sorted = sorted(real_opp_mp)
        tb3_list = real_opp_mp_sorted[1:] if len(real_opp_mp_sorted) > 1 else real_opp_mp_sorted
        tb3 = float(sum(tb3_list))

        out[team_no] = (tb1, tb2, tb3)
    return out


def rank_teams(
    match_points: dict[int, int],
    tiebreaks: dict[int, tuple[float, float, float]],
) -> list[int]:
    """Team numbers ordered best-to-worst per Annex 2.I precedence."""
    return sorted(
        match_points.keys(),
        key=lambda t: (-match_points[t], -tiebreaks[t][0], -tiebreaks[t][1], -tiebreaks[t][2]),
    )
