"""These guard exactly the incident that got our IP blocked by
chess-results.com: a recurring sync must not re-fetch data that can't have
changed. Everything here is mocked -- no real network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from chessolympiad.data import chessresults_client as cr
from chessolympiad.data import db
from chessolympiad.data.sync import sync_tournament


TID, TNR, SECTION, YEAR = "test-open", 999999, "open", 2026


def _meta(num_teams=3, num_rounds=3):
    return cr.TournamentMeta(
        tnr=TNR,
        name="Test Event",
        fide_event_id=None,
        num_rounds=num_rounds,
        num_teams=num_teams,
        teams=[
            {
                "team_no": i,
                "federation": "USA",
                "team_name": f"Team {i}",
                "rating_avg": 2500,
                "captain": None,
                "initial_rank": i,
            }
            for i in range(1, num_teams + 1)
        ],
    )


def _roster(team_no):
    return [
        {
            "board_no": 1,
            "title": "GM",
            "name": f"Player {team_no}",
            "rating": 2600,
            "federation": "USA",
            "fide_id": 1000 + team_no,
        }
    ]


def _game_row(round_no, team_a, team_b, result="1-0"):
    return {
        "round": round_no,
        "team_a_no": team_a,
        "team_b_no": team_b,
        "board_no": 1,
        "white_team_no": team_a,
        "white_fide_id": None,
        "white_name": f"Player {team_a}",
        "white_rating": 2600,
        "black_fide_id": None,
        "black_name": f"Player {team_b}",
        "black_rating": 2600,
        "result": result,
        "forfeit": False,
    }


@pytest.fixture
def patched(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    real_connect = db.connect
    monkeypatch.setattr("chessolympiad.data.sync.db.connect", lambda *a, **k: real_connect(db_path))

    fetch_teams = MagicMock(return_value=_meta())
    fetch_team_roster = MagicMock(side_effect=lambda tnr, team_no: _roster(team_no))
    fetch_round_board_results = MagicMock(return_value=[])
    monkeypatch.setattr("chessolympiad.data.sync.cr.fetch_teams", fetch_teams)
    monkeypatch.setattr("chessolympiad.data.sync.cr.fetch_team_roster", fetch_team_roster)
    monkeypatch.setattr("chessolympiad.data.sync.cr.fetch_round_board_results", fetch_round_board_results)
    return fetch_teams, fetch_team_roster, fetch_round_board_results, db_path


def test_fetch_rosters_false_skips_all_roster_requests(patched):
    fetch_teams, fetch_team_roster, fetch_round_board_results, db_path = patched
    sync_tournament(TID, TNR, SECTION, YEAR, fetch_rosters=False, refetch_complete_rounds=False)
    assert fetch_team_roster.call_count == 0


def test_recurring_sync_does_not_refetch_a_fully_decided_round(patched):
    fetch_teams, fetch_team_roster, fetch_round_board_results, db_path = patched

    # Round 1 complete, round 2 not yet played.
    fetch_round_board_results.side_effect = lambda tnr, rd: (
        [_game_row(1, 1, 2), _game_row(1, 3, 1)] if rd == 1 else []
    )
    sync_tournament(TID, TNR, SECTION, YEAR, fetch_rosters=True, refetch_complete_rounds=True)
    assert fetch_round_board_results.call_count == 2  # round 1 (data), round 2 (empty -> stop)

    # Next cycle: round 1 is now fully decided in the DB, round 2 still open.
    fetch_round_board_results.reset_mock()
    fetch_round_board_results.side_effect = lambda tnr, rd: (
        [_game_row(2, 1, 3)] if rd == 2 else []
    )
    sync_tournament(TID, TNR, SECTION, YEAR, fetch_rosters=False, refetch_complete_rounds=False)
    fetched_rounds = [call.args[1] for call in fetch_round_board_results.call_args_list]
    assert fetched_rounds == [2, 3], (
        f"expected only round 2 (still live) and round 3 (empty probe for a new round) to be "
        f"fetched, got {fetched_rounds} -- round 1 is already fully decided and should have "
        "been skipped"
    )


def test_recurring_sync_request_count_does_not_grow_as_rounds_complete(patched):
    """The bug this guards: without skipping complete rounds, a recurring
    sync's request count grows by one every time a new round finishes,
    eventually exceeding the daily cap on its own even with rosters
    excluded. Simulate the whole 3-round event finishing one round per
    cycle and assert the per-cycle round-request count stays bounded
    (only ever the live round + one empty probe for the next), not
    growing round-over-round.
    """
    fetch_teams, fetch_team_roster, fetch_round_board_results, db_path = patched
    fetch_teams.return_value = _meta(num_teams=3, num_rounds=5)  # headroom past round 3
    completed_rounds: dict[int, list[dict]] = {}

    def fake_fetch(tnr, rd):
        return completed_rounds.get(rd, [])

    fetch_round_board_results.side_effect = fake_fetch

    per_cycle_request_counts = []
    for rd in (1, 2, 3):
        completed_rounds[rd] = [_game_row(rd, 1, 2), _game_row(rd, 3, 1)]
        fetch_round_board_results.reset_mock()
        sync_tournament(TID, TNR, SECTION, YEAR, fetch_rosters=False, refetch_complete_rounds=False)
        per_cycle_request_counts.append(fetch_round_board_results.call_count)

    assert per_cycle_request_counts == [2, 2, 2], (
        f"expected a flat 2 requests/cycle (the newly-live round + one empty probe) "
        f"regardless of how many earlier rounds are already final, got {per_cycle_request_counts}"
    )


def test_fide_id_lookup_survives_a_sync_that_skips_roster_refetch(patched):
    """A recurring sync that skips --full-refresh must still link new
    games to FIDE IDs using rosters fetched in an *earlier* call -- not
    just rosters fetched in the current call.
    """
    fetch_teams, fetch_team_roster, fetch_round_board_results, db_path = patched

    fetch_round_board_results.return_value = []
    sync_tournament(TID, TNR, SECTION, YEAR, fetch_rosters=True, refetch_complete_rounds=True)
    assert fetch_team_roster.call_count == 3  # rosters ingested once, for all 3 teams

    fetch_team_roster.reset_mock()
    fetch_round_board_results.side_effect = lambda tnr, rd: [_game_row(1, 1, 2)] if rd == 1 else []
    sync_tournament(TID, TNR, SECTION, YEAR, fetch_rosters=False, refetch_complete_rounds=False)
    assert fetch_team_roster.call_count == 0  # confirms rosters truly weren't re-fetched this call

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT white_fide_id, black_fide_id FROM games WHERE tournament_id = ? AND round = 1",
            (TID,),
        ).fetchone()
    finally:
        conn.close()
    assert row["white_fide_id"] == 1001  # team 1's roster player, fetched in the earlier call
    assert row["black_fide_id"] == 1002  # team 2's roster player, fetched in the earlier call


def test_a_not_yet_known_round_is_always_probed_regardless_of_schedule(patched):
    """chess-results.com routinely publishes a round's pairings hours (or
    a day) before it officially starts -- a round not yet known locally
    must always be probed, never gated on the official start time (this
    is the regression: round 4's pairings went unnoticed for hours
    because an earlier version blocked exactly this).
    """
    fetch_teams, fetch_team_roster, fetch_round_board_results, db_path = patched
    # No results yet, but pairings (result=None) are already published --
    # exactly what chess-results.com looks like before a round starts.
    fetch_round_board_results.side_effect = lambda tnr, rd: (
        [_game_row(1, 1, 2, result=None)] if rd == 1 else []
    )

    sync_tournament("2026-open", TNR, SECTION, YEAR, fetch_rosters=False, refetch_complete_rounds=False)

    fetched_rounds = [call.args[1] for call in fetch_round_board_results.call_args_list]
    assert fetched_rounds == [1, 2], (
        f"expected round 1 (pairings, no results yet) to be fetched and round 2 probed, got {fetched_rounds}"
    )
