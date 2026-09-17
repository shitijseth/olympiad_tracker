import random

from chessolympiad.pairing.swiss_team import TeamPairingState
from chessolympiad.simulate.round import TournamentState, apply_boundary_round


def _team_state(team_nos):
    pairing_states = {no: TeamPairingState(team_no=no, seed=no) for no in team_nos}
    rosters = {no: [{"board_no": b, "fide_id": no * 10 + b, "name": f"P{no}.{b}", "rating": 2500} for b in range(1, 5)] for no in team_nos}
    return TournamentState(pairing_states=pairing_states, rosters=rosters, team_ratings={no: 2500 for no in team_nos})


def _game(a, b, board_no, white_team, white_id, white_name, white_rtg, black_id, black_name, black_rtg, result):
    return {
        "team_a_no": a, "team_b_no": b, "board_no": board_no, "white_team_no": white_team,
        "white_fide_id": white_id, "white_name": white_name, "white_rating": white_rtg,
        "black_fide_id": black_id, "black_name": black_name, "black_rating": black_rtg,
        "result": result,
    }


def test_known_boards_are_locked_in_unknown_boards_get_sampled():
    state = _team_state([1, 2])
    games = [
        _game(1, 2, 1, 1, 11, "P1.1", 2500, 21, "P2.1", 2500, "1-0"),   # known: team1 wins
        _game(1, 2, 2, 2, 22, "P2.2", 2500, 12, "P1.2", 2500, "0-1"),   # known: team1 (black) wins
        _game(1, 2, 3, 1, 13, "P1.3", 2500, 23, "P2.3", 2500, ""),      # unknown -- must be sampled
        _game(1, 2, 4, 2, 24, "P2.4", 2500, 14, "P1.4", 2500, None),    # unknown -- must be sampled
    ]
    rng = random.Random(42)
    apply_boundary_round(state, games, remaining_rounds_after=5, rng=rng)

    # Team 1 already has 2/2 known game points locked in; total game points
    # (and therefore match points) must reflect the sampled boards too, not
    # just the known ones -- i.e. the round is fully resolved, not partial.
    assert state.history[1][-1].game_points + state.history[2][-1].game_points == 4.0
    assert state.match_points[1] + state.match_points[2] in (2, 3)  # 2-0, 1-1 (never a "half" match yet unresolved)

    # Every board -- known and sampled alike -- must contribute to player
    # stats, or a still-open board would silently vanish from TPR tracking.
    for team_no, board_no in [(1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3), (1, 4), (2, 4)]:
        key = (team_no, board_no, str(team_no * 10 + board_no))
        assert key in state.player_stats, f"missing player_stats for {key}"
        assert state.player_stats[key].games == 1


def test_deterministic_given_same_seed():
    games = [
        _game(1, 2, 1, 1, 11, "P1.1", 2700, 21, "P2.1", 2300, "1-0"),
        _game(1, 2, 2, 2, 22, "P2.2", 2300, 12, "P1.2", 2700, ""),
        _game(1, 2, 3, 1, 13, "P1.3", 2700, 23, "P2.3", 2300, ""),
        _game(1, 2, 4, 2, 24, "P2.4", 2300, 14, "P1.4", 2700, ""),
    ]

    def run(seed):
        state = _team_state([1, 2])
        apply_boundary_round(state, games, remaining_rounds_after=5, rng=random.Random(seed))
        return state.match_points[1], state.match_points[2]

    assert run(7) == run(7)


def test_all_boards_already_known_still_resolves_the_match():
    state = _team_state([1, 2])
    games = [
        _game(1, 2, 1, 1, 11, "P1.1", 2500, 21, "P2.1", 2500, "1-0"),
        _game(1, 2, 2, 2, 22, "P2.2", 2500, 12, "P1.2", 2500, "1-0"),
        _game(1, 2, 3, 1, 13, "P1.3", 2500, 23, "P2.3", 2500, "1-0"),
        _game(1, 2, 4, 2, 24, "P2.4", 2500, 14, "P1.4", 2500, "1-0"),
    ]
    apply_boundary_round(state, games, remaining_rounds_after=5, rng=random.Random(1))
    # Team 1: won boards 1,3 as white (1-0 each) and boards 2,4 as black
    # (both were "1-0" for the white side, team 2) -- team1 game points = 2.
    assert state.history[1][-1].game_points == 2.0
    assert state.match_points[1] == 1 and state.match_points[2] == 1


def test_unknown_board_win_rate_matches_rating_gap_over_many_samples():
    games_template = [
        _game(1, 2, 1, 1, 11, "P1.1", 2900, 21, "P2.1", 1900, ""),
    ]
    wins = 0
    n = 4000
    for i in range(n):
        state = _team_state([1, 2])
        apply_boundary_round(state, games_template, remaining_rounds_after=5, rng=random.Random(i))
        if state.match_points[1] == 2:
            wins += 1
    # A 1000-point rating gap should be an overwhelming favorite; this is a
    # loose sanity bound, not a precise calibration check (that's elo's own
    # test suite's job).
    assert wins / n > 0.9
