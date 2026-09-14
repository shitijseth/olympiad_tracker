import random

from chessolympiad.model.lineup import active_lineup, choose_lineup


def _roster():
    return [
        {"board_no": 1, "fide_id": 1, "name": "A", "rating": 2700},
        {"board_no": 2, "fide_id": 2, "name": "B", "rating": 2650},
        {"board_no": 3, "fide_id": 3, "name": "C", "rating": 2600},
        {"board_no": 4, "fide_id": 4, "name": "D", "rating": 2550},
        {"board_no": 5, "fide_id": 5, "name": "Reserve", "rating": 2400},
    ]


def test_no_reserve_means_no_substitution():
    roster = [p for p in _roster() if p["board_no"] <= 4]
    rng = random.Random(0)
    lineup = choose_lineup(roster, 2625, 1800, rng)
    assert [p["name"] for p in lineup] == ["A", "B", "C", "D"]


def test_substitution_rate_rises_against_much_weaker_opponent():
    roster = _roster()
    rng = random.Random(42)
    n = 4000
    subs_vs_weak = sum(1 for _ in range(n) if "Reserve" in [p["name"] for p in choose_lineup(roster, 2625, 1900, rng)])
    subs_vs_even = sum(1 for _ in range(n) if "Reserve" in [p["name"] for p in choose_lineup(roster, 2625, 2625, rng)])
    assert subs_vs_weak > subs_vs_even, "captains should rest a board more often against a much weaker opponent"


def test_active_lineup_ignores_reserve():
    lineup = active_lineup(_roster())
    assert len(lineup) == 4
    assert all(p["board_no"] <= 4 for p in lineup)
