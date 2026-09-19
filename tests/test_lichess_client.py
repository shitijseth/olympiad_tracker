"""Everything here is mocked -- no real network calls to lichess.org."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from chessolympiad.data import lichess_client as lc

SAMPLE_PGN = """\
[Event "Olymp 2026 Open"]
[Round "4.169"]
[White "Abdusattorov, Nodirbek"]
[Black "Erdogmus, Yagiz Kaan"]
[Result "1/2-1/2"]
[WhiteElo "2762"]
[WhiteTeam "Uzbekistan"]
[WhiteFideId "14204118"]
[BlackElo "2716"]
[BlackTeam "Turkiye"]
[BlackFideId "44599790"]

1. d4 Nf6 1/2-1/2

[Event "Olymp 2026 Open"]
[Round "4.170"]
[White "Gurel, Ediz"]
[Black "Sindarov, Javokhir"]
[Result "*"]
[WhiteElo "2636"]
[WhiteTeam "Turkiye"]
[WhiteFideId "44507356"]
[BlackElo "2778"]
[BlackTeam "Uzbekistan"]
[BlackFideId "14205483"]

1. e4 *
"""


def test_fetch_round_games_parses_headers_and_maps_unresolved_result_to_none(monkeypatch):
    resp = MagicMock(text=SAMPLE_PGN)
    monkeypatch.setattr(lc, "_get", MagicMock(return_value=resp))

    games = lc.fetch_round_games("someRoundId")
    assert len(games) == 2

    g1, g2 = games
    assert g1["round"] == 4
    assert g1["white_name"] == "Abdusattorov, Nodirbek"
    assert g1["white_team_name"] == "Uzbekistan"
    assert g1["black_team_name"] == "Turkiye"
    assert g1["white_fide_id"] == 14204118
    assert g1["black_fide_id"] == 44599790
    assert g1["white_rating"] == 2762
    assert g1["result"] == "1/2-1/2"

    # A game still in progress ("*" in the PGN, standard "unknown result"
    # token) must come out as None, not the literal "*" -- matching NULL
    # in the games table, same as chess-results.com's blank result cell.
    assert g2["result"] is None


def test_fetch_round_games_handles_missing_fide_id(monkeypatch):
    pgn = SAMPLE_PGN.replace('[WhiteFideId "14204118"]\n', "")
    resp = MagicMock(text=pgn)
    monkeypatch.setattr(lc, "_get", MagicMock(return_value=resp))
    games = lc.fetch_round_games("someRoundId")
    assert games[0]["white_fide_id"] is None


def test_fetch_broadcast_rounds_parses_round_number_from_name(monkeypatch):
    resp = MagicMock()
    resp.json.return_value = {
        "rounds": [
            {"id": "abc123", "name": "Round 1", "finished": True},
            {"id": "def456", "name": "Round 2", "finished": False},
            {"id": "ghi789", "name": "Opening Ceremony"},  # non-standard name -- must be skipped, not crash
        ]
    }
    monkeypatch.setattr(lc, "_get", MagicMock(return_value=resp))

    rounds = lc.fetch_broadcast_rounds("someBroadcastId")
    assert len(rounds) == 2
    assert rounds[0].id == "abc123" and rounds[0].number == 1 and rounds[0].finished is True
    assert rounds[1].id == "def456" and rounds[1].number == 2 and rounds[1].finished is False


def test_get_raises_rate_limited_on_429(monkeypatch):
    resp = MagicMock(status_code=429)
    monkeypatch.setattr(lc._session, "get", MagicMock(return_value=resp))
    with pytest.raises(lc.RateLimited):
        lc._get("/api/broadcast/whatever")


def test_get_does_not_retry_after_rate_limited(monkeypatch):
    """A 429 must surface immediately as an exception the caller can catch
    and stop on for this cycle -- never silently retried in a hot loop
    (see the incident that got the other data source blocked)."""
    call_count = 0

    def fake_get(*a, **k):
        nonlocal call_count
        call_count += 1
        return MagicMock(status_code=429)

    monkeypatch.setattr(lc._session, "get", fake_get)
    with pytest.raises(lc.RateLimited):
        lc._get("/api/broadcast/whatever")
    assert call_count == 1
