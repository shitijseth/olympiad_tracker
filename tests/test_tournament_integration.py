"""End-to-end sanity check: a Monte Carlo run over a tiered synthetic field
must actually stratify by strength. This guards against the class of bug
where the pairing engine's internal score-group state silently stops
tracking real match points (which happened once: TeamPairingState.match_points
must be kept in sync with TournamentState.match_points every round, or Swiss
pairing degenerates into round-1-forever and mid-table teams start looking
as likely to win gold as the top seed).
"""

from chessolympiad.simulate.tournament import TeamInfo, run_monte_carlo


def _make_field():
    # 32 teams: 8 "elite" (~2750), 8 "mid" (~2400), 16 "weak" (~1700).
    teams = []
    rosters = {}
    tiers = [(2750, 8), (2400, 8), (1700, 16)]
    team_no = 1
    for rating, count in tiers:
        for _ in range(count):
            teams.append(TeamInfo(team_no=team_no, federation=f"F{team_no}", team_name=f"Team{team_no}", initial_rank=team_no, rating_avg=rating))
            rosters[team_no] = [{"board_no": b, "fide_id": team_no * 10 + b, "name": f"P{team_no}.{b}", "rating": rating} for b in range(1, 5)]
            team_no += 1
    teams.sort(key=lambda t: t.initial_rank)
    return teams, rosters


def test_elite_teams_dominate_final_standings():
    teams, rosters = _make_field()
    forecasts, _players = run_monte_carlo(teams, rosters, num_rounds=9, iterations=300, seed=7)

    elite_avg_rank = sum(forecasts[t.team_no].expected_rank for t in teams[:8]) / 8
    weak_avg_rank = sum(forecasts[t.team_no].expected_rank for t in teams[16:]) / 16
    assert elite_avg_rank < weak_avg_rank - 5, "elite tier should finish clearly ahead of the weak tier on average"

    elite_gold = sum(forecasts[t.team_no].p_gold for t in teams[:8])
    weak_gold = sum(forecasts[t.team_no].p_gold for t in teams[16:])
    assert elite_gold > weak_gold * 3, "gold-medal probability mass should concentrate heavily on the elite tier"

    # no single weak team should have a gold chance anywhere close to a top seed's
    best_elite_gold = max(forecasts[t.team_no].p_gold for t in teams[:8])
    best_weak_gold = max(forecasts[t.team_no].p_gold for t in teams[16:])
    assert best_weak_gold < best_elite_gold, "a 1700-rated team must not out-forecast a 2750-rated team for gold"
