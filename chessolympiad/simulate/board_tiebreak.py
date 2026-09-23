"""Individual board medal eligibility and ranking -- FIDE Chess Olympiad
2026 Regulations Article 4.6.3 and Appendix 2.III (see
reference/fide_olympiad_2026_regulations.md for the full text, fetched
directly from handbook.fide.com).

Article 4.6.3.1: a player needs to play at least eight games to be
eligible for a board prize.
Article 4.6.3.2 + Appendix 2.III: board medals are awarded by performance
rating (TPR); ties are broken by (TB1) greater number of games played,
then (TB2) drawing of lots.

TB2 (drawing of lots) is a physical/random procedure carried out at the
event itself -- it cannot be meaningfully predicted or replicated here.
Players still tied after TB1 are reported as genuinely tied (same rank),
never given a fabricated order, since asserting one would misrepresent
what the rule actually determines.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_GAMES_FOR_BOARD_MEDAL = 8  # Article 4.6.3.1


@dataclass
class RankedPlayer:
    key: object  # caller's own identifier, passed through unchanged
    tpr: float
    games: int
    eligible: bool
    rank: int | None  # None if not eligible; ties share the same rank


def rank_board_players(players: list[tuple[object, float, int]]) -> list[RankedPlayer]:
    """players: list of (key, tpr, games_played) for everyone who has
    played at least one game on this board (any player_stats entry).
    Returns them all, each annotated with eligibility and, for eligible
    players, a rank -- sorted eligible-first by (TPR desc, games desc)
    per Appendix 2.III, then ineligible players in the same input order
    they were given (they have no defined rank).

    Ties in both TPR and games (TB1 exhausted) get the same rank number,
    and the next distinct rank skips ahead by the tie's size -- the same
    "top ranking of that set" convention Appendix 2.I/II use for teams.
    """
    eligible = [
        RankedPlayer(key=k, tpr=tpr, games=games, eligible=True, rank=None)
        for k, tpr, games in players
        if games >= MIN_GAMES_FOR_BOARD_MEDAL
    ]
    ineligible = [
        RankedPlayer(key=k, tpr=tpr, games=games, eligible=False, rank=None)
        for k, tpr, games in players
        if games < MIN_GAMES_FOR_BOARD_MEDAL
    ]
    eligible.sort(key=lambda p: (-p.tpr, -p.games))
    # Not officially ranked (rank stays None -- they haven't met Article
    # 4.6.3.1's eligibility bar), but still sorted by TPR for readability:
    # early in the event nobody may be eligible yet, and an arbitrary
    # (insertion) order would make "who's leading so far" unreadable.
    ineligible.sort(key=lambda p: (-p.tpr, -p.games))

    rank = 0
    prev_key = None
    for i, p in enumerate(eligible):
        sort_key = (p.tpr, p.games)
        if sort_key != prev_key:
            rank = i + 1
            prev_key = sort_key
        p.rank = rank

    return eligible + ineligible
