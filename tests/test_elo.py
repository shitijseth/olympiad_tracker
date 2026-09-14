from chessolympiad.model import elo


def test_probabilities_sum_to_one():
    for rw, rb in [(2500, 2500), (2000, 2800), (1200, 2900), (2650, 2400)]:
        pw, pd, pb = elo.outcome_probs(rw, rb)
        assert abs((pw + pd + pb) - 1.0) < 1e-9
        assert pw > 0 and pd > 0 and pb > 0


def test_higher_rating_wins_more_often():
    pw1, _, pb1 = elo.outcome_probs(2700, 2700)
    pw2, _, pb2 = elo.outcome_probs(2900, 2500)
    assert pw2 > pw1
    assert pb2 < pb1


def test_draw_rate_increases_with_average_rating():
    _, pd_low, _ = elo.outcome_probs(1500, 1500)
    _, pd_high, _ = elo.outcome_probs(2750, 2750)
    assert pd_high > pd_low


def test_white_advantage_makes_white_favorite_at_equal_rating():
    pw, _pd, pb = elo.outcome_probs(2500, 2500)
    assert pw > pb


def test_sample_result_only_returns_valid_outcomes():
    import random

    rng = random.Random(42)
    for _ in range(200):
        result = elo.sample_result(2600, 2550, rng)
        assert result in ("1-0", "1/2-1/2", "0-1")
