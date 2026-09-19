"""Regression coverage for chessresults_client.fetch_teams's column-layout
parsing -- everything here is a synthetic in-memory .xlsx, no real network
calls.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pandas as pd

from chessolympiad.data import chessresults_client as cr


def _xlsx_bytes(rows: list[list]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, header=False, engine="openpyxl")
    return buf.getvalue()


def _mock_response(content: bytes):
    resp = MagicMock()
    resp.content = content
    return resp


def test_fetch_teams_finds_captain_past_the_group_column(monkeypatch):
    """The live 2026 export layout: No. | (blank) | FED | Team | RtgAvg |
    Group | Captain -- an extra "Group" (division letter) column sits
    between RtgAvg and Captain. Reading captain as rtg_col + 1 (an older
    assumption) picks up "Group"'s A/B letter for every team instead of
    the real captain name -- this is the exact bug reported live.
    """
    rows = [
        ["Tournament ABC"],
        ["Number of rounds : 11"],
        ["No.", None, "FED", "Team", "RtgAvg", "Group", "Captain"],
        [1, None, "USA", "United States of America", 2753, "A", "John Donaldson"],
        [2, None, "IND", "India", 2735, "A", "Srinath Narayanan"],
    ]
    monkeypatch.setattr(cr, "_get", MagicMock(return_value=_mock_response(_xlsx_bytes(rows))))

    meta = cr.fetch_teams(tnr=1)
    assert meta.teams[0]["captain"] == "John Donaldson"
    assert meta.teams[1]["captain"] == "Srinath Narayanan"
    assert meta.teams[0]["team_name"] == "United States of America"
    assert meta.teams[0]["federation"] == "USA"
    assert meta.teams[0]["rating_avg"] == 2753


def test_fetch_teams_finds_captain_with_no_group_column(monkeypatch):
    """An older/simpler layout with no "Group" column at all -- Captain
    immediately after RtgAvg -- must keep working too.
    """
    rows = [
        ["Tournament ABC"],
        ["Number of rounds : 11"],
        ["No.", None, "FED", "Team", "RtgAvg", "Captain"],
        [1, None, "USA", "United States of America", 2753, "John Donaldson"],
    ]
    monkeypatch.setattr(cr, "_get", MagicMock(return_value=_mock_response(_xlsx_bytes(rows))))

    meta = cr.fetch_teams(tnr=1)
    assert meta.teams[0]["captain"] == "John Donaldson"
