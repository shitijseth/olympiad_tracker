"""FIDE Chess Olympiad 2026 Regulations Article 4.6.3 + Appendix 2.III --
see reference/fide_olympiad_2026_regulations.md for the source text.
"""

from __future__ import annotations

from chessolympiad.simulate.board_tiebreak import MIN_GAMES_FOR_BOARD_MEDAL, rank_board_players


def test_players_below_the_minimum_games_are_ineligible_and_unranked():
    players = [("a", 2700.0, MIN_GAMES_FOR_BOARD_MEDAL - 1), ("b", 2500.0, MIN_GAMES_FOR_BOARD_MEDAL)]
    ranked = {p.key: p for p in rank_board_players(players)}
    assert ranked["a"].eligible is False
    assert ranked["a"].rank is None
    assert ranked["b"].eligible is True
    assert ranked["b"].rank == 1


def test_exactly_the_minimum_games_is_eligible():
    players = [("a", 2600.0, MIN_GAMES_FOR_BOARD_MEDAL)]
    ranked = rank_board_players(players)
    assert ranked[0].eligible is True
    assert ranked[0].rank == 1


def test_sorted_by_tpr_descending_among_eligible_players():
    players = [("low", 2500.0, 10), ("high", 2700.0, 8), ("mid", 2600.0, 9)]
    ranked = rank_board_players(players)
    assert [p.key for p in ranked] == ["high", "mid", "low"]
    assert [p.rank for p in ranked] == [1, 2, 3]


def test_equal_tpr_broken_by_more_games_played_tb1():
    players = [("fewer_games", 2600.0, 8), ("more_games", 2600.0, 11)]
    ranked = rank_board_players(players)
    assert [p.key for p in ranked] == ["more_games", "fewer_games"]
    assert ranked[0].rank == 1
    assert ranked[1].rank == 2


def test_tied_after_tb1_share_the_same_rank_and_next_rank_skips_the_tie_size():
    # Equal TPR and equal games -- TB2 is drawing of lots, a physical
    # procedure this codebase cannot predict, so both stay genuinely tied.
    players = [("a", 2600.0, 9), ("b", 2600.0, 9), ("c", 2500.0, 9)]
    ranked = {p.key: p for p in rank_board_players(players)}
    assert ranked["a"].rank == ranked["b"].rank == 1
    assert ranked["c"].rank == 3  # skips rank 2 -- two players already hold rank 1


def test_ineligible_players_are_still_sorted_by_tpr_for_readability():
    # Not officially ranked (rank=None -- they haven't met the eligibility
    # bar), but still ordered sensibly rather than left in arbitrary
    # (insertion) order -- useful early in an event when nobody may be
    # eligible yet.
    players = [("z", 2400.0, 3), ("y", 2900.0, 1)]
    ranked = rank_board_players(players)
    assert [p.key for p in ranked] == ["y", "z"]
    assert all(p.rank is None for p in ranked)


def test_empty_input():
    assert rank_board_players([]) == []
