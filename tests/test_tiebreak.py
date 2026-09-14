from chessolympiad.simulate.tiebreak import RoundRecord, compute_tiebreaks, rank_teams


def test_hand_computed_is10_two_opponents():
    # Team 1 beat Team 2 (3-1 on game points) then drew Team 3 (2-2).
    # Final match points: team1=3 (2+1), team2=4 (from other results), team3=2.
    history = {
        1: [
            RoundRecord(opponent_no=2, game_points=3.0, cmp_before=0, remaining_rounds_after=1),
            RoundRecord(opponent_no=3, game_points=2.0, cmp_before=2, remaining_rounds_after=0),
        ]
    }
    final_mp = {1: 3, 2: 4, 3: 2}
    tb = compute_tiebreaks(history, final_mp)
    tb1, tb2, tb3 = tb[1]
    # Lowest-scoring opponent (team 3, final MP=2) is dropped; only team 2 (MP=4) counted.
    assert tb1 == 3.0 * 4  # IS_2 = game_points_vs_2 * final_mp_2
    assert tb2 == 5.0  # total game points 3+2
    assert tb3 == 4.0  # best-remaining opponent's final MP


def test_bye_uses_regulation_formula():
    # A pairing-allocated bye: IS(b) = 2 x (CMP + 1 + RR)
    history = {1: [RoundRecord(opponent_no=None, game_points=4.0, cmp_before=6, remaining_rounds_after=3, kind="bye")]}
    final_mp = {1: 8}
    tb1, tb2, tb3 = compute_tiebreaks(history, final_mp)[1]
    assert tb1 == 2 * (6 + 1 + 3)
    assert tb2 == 4.0
    assert tb3 == 0.0  # no real opponent to contribute to TB3


def test_rank_teams_orders_by_match_points_then_tiebreaks():
    match_points = {1: 8, 2: 8, 3: 6}
    tiebreaks = {1: (10.0, 5.0, 3.0), 2: (12.0, 4.0, 2.0), 3: (0.0, 0.0, 0.0)}
    ranking = rank_teams(match_points, tiebreaks)
    assert ranking == [2, 1, 3]  # team 2 wins the MP tie via higher TB1
