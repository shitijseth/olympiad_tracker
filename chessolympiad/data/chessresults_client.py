"""Client for chess-results.com -- the single canonical data source for both
the live 2026 Olympiad and the historical 2022/2024 events used to calibrate
the model.

All URL patterns and HTML/Excel structures used here were confirmed by
direct inspection of the live site (2026 Open tnr=1469895, 2026 Women
tnr=1469896, 2024 Budapest Open tnr=967173, 2024 Budapest Women tnr=967172,
2022 Chennai Open tnr=653631, 2022 Chennai Women tnr=653632). Key views
(all addressed via the `art=` query param):

    art=32  team starting rank / seed list (supports a clean .xlsx export
            via &prt=4&excel=2010)
    art=8   one team's roster ("Team-Composition"), &snr=<team_no>
    art=3   full round board-by-board results, &rd=<round>&zeilen=99999

Everything here is read-only HTTP GET against a public results server.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://chess-results.com/tnr{tnr}.aspx"
TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ChessOlympiadPredictor/1.0)"}
REQUEST_DELAY_SECONDS = 0.15  # be a polite citizen of a shared public results server

_session = requests.Session()
_session.headers.update(HEADERS)

# Fraction/forfeit tokens chess-results uses in result cells.
_RESULT_TOKENS = {
    "1": 1.0,
    "0": 0.0,
    "½": 0.5,
    "0.5": 0.5,
    "+": 1.0,   # forfeit win
    "-": 0.0,   # forfeit loss
}


class RateLimited(RuntimeError):
    """chess-results.com blocked this IP for exceeding its daily request cap.

    This is a manual, admin-issued block (per the site's own error page),
    not a rolling window -- it will not clear on its own. Unblocking
    requires emailing h.herzog@swiss-manager.at.
    """


def _get(tnr: int, params: dict) -> requests.Response:
    p = {"lan": 1, **params}
    resp = _session.get(BASE.format(tnr=tnr), params=p, timeout=TIMEOUT)
    resp.raise_for_status()
    # An expected .xlsx/binary export starts with "PK" (zip magic bytes);
    # if we got HTML back instead, chess-results.com served its own error
    # page (most notably the per-IP daily-limit block) instead of data.
    if params.get("excel") and not resp.content.startswith(b"PK"):
        if b"exceeded" in resp.content[:4000].lower() or b"daily limit" in resp.content[:4000].lower():
            raise RateLimited(
                "chess-results.com has rate-limited this IP (daily request cap exceeded). "
                "Email h.herzog@swiss-manager.at to request unblocking."
            )
        raise RuntimeError("chess-results.com returned an unexpected (non-Excel) response")
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp


@dataclass
class TournamentMeta:
    tnr: int
    name: str
    fide_event_id: Optional[int]
    num_rounds: int
    num_teams: int
    teams: list[dict] = field(default_factory=list)


def _infer_team_columns(df: "pd.DataFrame", start: int) -> tuple[int, int]:
    """Fallback for fetch_teams when the header row's column labels are
    blank: find (fed_col, rtg_col) from the shape of the actual data rows
    instead. rtg_col is the only column whose values sit in a plausible
    team-average-rating range across the first several data rows; fed_col
    is the first short all-caps federation-code column before it (skipping
    the blank spacer column at index 1 and, per the 2022 Chennai layout, a
    possible duplicate team-code column -- either one is a valid "FED").
    """
    sample = df.iloc[start : start + 10]
    rtg_col = None
    for c in range(1, df.shape[1]):
        vals = pd.to_numeric(sample[c], errors="coerce").dropna()
        if len(vals) >= max(1, len(sample) - 1) and vals.between(1000, 3200).all():
            rtg_col = c
            break
    if rtg_col is None:
        raise ValueError("Could not infer RtgAvg column from data rows")

    fed_col = None
    for c in range(1, rtg_col):
        vals = sample[c].dropna().astype(str)
        if len(vals) and vals.str.match(r"^[A-Z]{2,5}\d?$").all():
            fed_col = c
            break
    if fed_col is None:
        raise ValueError("Could not infer FED column from data rows")
    return fed_col, rtg_col


def fetch_teams(tnr: int) -> TournamentMeta:
    """Team starting-rank list + tournament metadata, via the .xlsx export.

    Returns TournamentMeta with .teams = [{team_no, federation, team_name,
    captain, rating_avg}, ...] in seed order (team_no = chess-results snr,
    used as the natural key for this team across all other views).
    """
    resp = _get(tnr, {"art": 32, "zeilen": 99999, "turdet": "YES", "flag": 30, "prt": 4, "excel": 2010})
    df = pd.read_excel(io.BytesIO(resp.content), header=None)
    col0 = df[0].astype(str)

    name = str(df.iloc[1, 0]) if len(df) > 1 else f"tnr{tnr}"
    fide_event_id = None
    num_rounds = 11
    for i in range(min(20, len(df))):
        cell = str(df.iloc[i, 0])
        m = re.search(r"FIDE-Event-ID\s*:\s*(\d+)", cell)
        if m:
            fide_event_id = int(m.group(1))
        m = re.search(r"Number of rounds\s*:\s*(\d+)", cell)
        if m:
            num_rounds = int(m.group(1))

    header_idx = col0[col0 == "No."].index
    if len(header_idx) == 0:
        raise ValueError(f"Could not locate team-list header row for tnr={tnr}")
    header_idx = header_idx[0]
    start = header_idx + 1

    # Column layout isn't stable across tournament editions (e.g. 2022
    # Chennai allowed multiple teams per federation and has an extra
    # "team code" column, duplicating the "Team" header). Locate columns by
    # label instead of a fixed index: the team-display-name column is
    # always immediately before "RtgAvg", and captain immediately after.
    #
    # chess-results.com started serving this header row with every label
    # but "No." blanked out (confirmed across a live 2026 event and two
    # long-finished historical ones alike, so it's a site-wide export
    # change, not a live-tournament quirk) -- fall back to inferring
    # columns from the first data row itself when that happens: RtgAvg is
    # the only column in a small team-rating range, and the docstring's
    # positional relationship (team name right before it, captain right
    # after) still holds regardless of how many federation/code columns
    # precede it.
    headers = df.iloc[header_idx].tolist()
    try:
        fed_col = headers.index("FED")
        rtg_col = headers.index("RtgAvg")
    except ValueError:
        fed_col, rtg_col = _infer_team_columns(df, start)
    team_col = rtg_col - 1
    captain_col = rtg_col + 1

    teams = []
    for i in range(start, len(df)):
        no_val = df.iloc[i, 0]
        if pd.isna(no_val) or not str(no_val).strip().isdigit():
            break
        rtg_val = df.iloc[i, rtg_col]
        cap_val = df.iloc[i, captain_col] if captain_col < df.shape[1] else None
        teams.append(
            {
                "team_no": int(no_val),
                "federation": str(df.iloc[i, fed_col]).strip(),
                "team_name": str(df.iloc[i, team_col]).strip(),
                "rating_avg": int(rtg_val) if not pd.isna(rtg_val) else None,
                "captain": None if pd.isna(cap_val) else str(cap_val).strip(),
                "initial_rank": int(no_val),
            }
        )
    return TournamentMeta(
        tnr=tnr,
        name=name,
        fide_event_id=fide_event_id,
        num_rounds=num_rounds,
        num_teams=len(teams),
        teams=teams,
    )


def fetch_team_roster(tnr: int, team_no: int) -> list[dict]:
    """One team's roster (board order, players, ratings, FIDE IDs)."""
    resp = _get(tnr, {"art": 8, "turdet": "YES", "flag": 30, "snr": team_no})
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", class_="CRs1")
    if table is None:
        return []
    rows = table.find_all("tr")
    players = []
    for tr in rows:
        cells = tr.find_all("td", recursive=False)
        # Column count varies: an upcoming tournament's roster has 7 cols
        # (Bo./Title/Name/Rtg/FED/FideID/Games); a finished one adds
        # Pts./Rp. The first 6 columns are stable across both -- match on
        # a numeric first cell (the board number) rather than an exact count.
        if len(cells) < 6 or not cells[0].get_text(strip=True).isdigit():
            continue  # header row or the team-name banner row
        board_no = int(cells[0].get_text(strip=True))
        title = cells[1].get_text(strip=True)
        name_link = cells[2].find("a")
        name = (name_link or cells[2]).get_text(strip=True)
        rtg_text = cells[3].get_text(strip=True)
        rating = int(rtg_text) if rtg_text.isdigit() else None
        fed = cells[4].get_text(strip=True)
        fide_link = cells[5].find("a")
        fide_text = fide_link.get_text(strip=True) if fide_link else cells[5].get_text(strip=True)
        fide_id = int(fide_text) if fide_text.isdigit() else None
        players.append(
            {
                "board_no": board_no,
                "title": title or None,
                "name": name,
                "rating": rating,
                "federation": fed or None,
                "fide_id": fide_id,
            }
        )
    return players


def _parse_score_cell(text: str) -> Optional[float]:
    text = text.strip()
    if text in _RESULT_TOKENS:
        return _RESULT_TOKENS[text]
    text = text.replace("½", ".5")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_round_board_results(tnr: int, round_no: int) -> list[dict]:
    """Full board-by-board results for one round, across every match.

    Returns rows shaped for the `games` table: team_a_no/team_b_no identify
    the match (team_a is whichever side is printed first by chess-results),
    board_no is 1-4 within the match, white_/black_ fields carry name,
    rating, and result is one of '1-0', '0-1', '1/2-1/2', or None if
    unparseable (adjourned edge cases are recorded with `forfeit`=True where
    the raw score was a +/- token).
    """
    resp = _get(
        tnr,
        {"art": 3, "rd": round_no, "zeilen": 99999, "turdet": "YES", "flag": 30},
    )
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", class_="CRs1")
    if table is None:
        return []

    rows_out = []
    team_a_no = team_b_no = None
    for tr in table.find_all("tr", recursive=False):
        ths = tr.find_all("th", recursive=False)
        if ths and len(ths) >= 9:
            # Match header row: Bo. | snrA | teamA | Rtg | - | snrB | teamB | Rtg | score | PGN
            try:
                team_a_no = int(ths[1].get_text(strip=True))
                team_b_no = int(ths[5].get_text(strip=True))
            except ValueError:
                team_a_no = team_b_no = None
            continue

        tds = tr.find_all("td", recursive=False)
        if len(tds) < 9:
            continue  # captain row or spacer row
        board_label = tds[0].get_text(strip=True)
        m = re.match(r"^\d+\.(\d+)$", board_label)
        if not m or team_a_no is None:
            continue  # captain/blank row, not a board row
        board_no = int(m.group(1))

        title_a = tds[1].get_text(strip=True) or None
        cell_a = tds[2]
        name_a = cell_a.get_text(strip=True)
        color_div_a = cell_a.find("div")
        a_is_white = bool(color_div_a and "FarbewT" in (color_div_a.get("class") or []))
        rating_a_text = tds[3].get_text(strip=True)
        rating_a = int(rating_a_text) if rating_a_text.isdigit() else None

        title_b = tds[5].get_text(strip=True) or None
        cell_b = tds[6]
        name_b = cell_b.get_text(strip=True)
        rating_b_text = tds[7].get_text(strip=True)
        rating_b = int(rating_b_text) if rating_b_text.isdigit() else None

        # Tokens are separated by " - " (space-dash-space); a lone "-" or "+"
        # token (forfeit) has no surrounding spaces of its own, so splitting
        # on the literal " - " separator (not just any dash) disambiguates
        # e.g. "- - +" (forfeit loss/win) from a normal "0 - 1".
        score_text = re.sub(r"\s+", " ", tds[8].get_text(strip=True))
        parts = score_text.split(" - ")
        score_a = _parse_score_cell(parts[0]) if len(parts) == 2 else None
        score_b = _parse_score_cell(parts[1]) if len(parts) == 2 else None
        is_forfeit = len(parts) == 2 and (parts[0] in ("+", "-") or parts[1] in ("+", "-"))

        if a_is_white:
            white_name, white_rating, white_title = name_a, rating_a, title_a
            black_name, black_rating, black_title = name_b, rating_b, title_b
            white_score, black_score = score_a, score_b
            white_team_no = team_a_no
        else:
            white_name, white_rating, white_title = name_b, rating_b, title_b
            black_name, black_rating, black_title = name_a, rating_a, title_a
            white_score, black_score = score_b, score_a
            white_team_no = team_b_no

        if white_score == 1.0 and black_score == 0.0:
            result = "1-0"
        elif white_score == 0.0 and black_score == 1.0:
            result = "0-1"
        elif white_score == 0.5 and black_score == 0.5:
            result = "1/2-1/2"
        else:
            result = None  # unplayed / adjourned / unrecognized token

        rows_out.append(
            {
                "round": round_no,
                "team_a_no": team_a_no,
                "team_b_no": team_b_no,
                "board_no": board_no,
                "white_team_no": white_team_no,
                "white_fide_id": None,  # filled in by a name-join pass, see data.sync
                "white_name": white_name,
                "white_rating": white_rating,
                "black_fide_id": None,
                "black_name": black_name,
                "black_rating": black_rating,
                "result": result,
                "forfeit": is_forfeit,
            }
        )
    return rows_out
