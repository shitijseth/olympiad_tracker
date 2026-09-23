"""Client for lichess.org's broadcast API -- the primary live results source
for the 2026 Olympiad (see chessolympiad/data/lichess_sync.py for why: it's
the same official live-boards feed the venue uses, but Lichess publishes it
minutes ahead of chess-results.com's arbiter-entered results, and -- unlike
chess-results.com -- explicitly welcomes automated polling with no daily cap).

The full 400+-team field is split across several "sub-broadcast" tournaments
(Lichess caps a single broadcast at 100 games/round), discovered once by
inspecting https://lichess.org/broadcast/46th-fide-chess-olympiad-samarkand-2026
and hardcoded below -- there is no API to discover these automatically, and
they will not change during the event.

Etiquette (see https://lichess.org/page/api-tips and the incident that got
this project's other data source blocked): requests are strictly sequential
(never concurrent), a small delay is enforced between them, and a 429 is
treated as "back off for the rest of this cycle", never retried in a hot
loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

BASE = "https://lichess.org"
# A healthy response normally lands in ~1-2s (measured directly against
# the live broadcast); 30s let a single degraded request eat up to ~60s
# (requests applies this to connect and read separately), and with several
# sequential sub-broadcast requests per round that compounded into a
# tick-stalling-for-many-minutes worst case in production. 10s still gives
# real headroom over the normal case while capping that compounding.
TIMEOUT = 10
HEADERS = {"User-Agent": "ChessOlympiadPredictor/1.0 (contact: shitijseth@arizona.edu)"}
REQUEST_DELAY_SECONDS = 1.0  # deliberately more conservative than chess-results.com's

# Discovered once from the broadcast hub page's sub-broadcast links; stable
# for the life of the event (Lichess broadcast IDs don't change).
BROADCAST_IDS: dict[str, list[str]] = {
    "open": ["n1pPI5Q0", "MSQXIzkK", "Uvg2Lyjt", "sfWvd9Pd", "MyBFv3Ha"],
    "women": ["HtMn014k", "UjEVXNHv", "JyQqARgN", "oQuU2arG"],
}

_session = requests.Session()
_session.headers.update(HEADERS)


class RateLimited(RuntimeError):
    """lichess.org returned 429. Callers should stop for this cycle, not retry."""


def _get(path: str, **params) -> requests.Response:
    resp = _session.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    if resp.status_code == 429:
        raise RateLimited(f"lichess.org rate-limited {path} -- back off, do not retry now")
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp


@dataclass
class BroadcastRound:
    id: str
    number: int
    finished: bool


def fetch_broadcast_rounds(broadcast_id: str) -> list[BroadcastRound]:
    """The rounds of one sub-broadcast tournament, with their Lichess round
    IDs and completion status. Cheap (a few KB), meant to be cached (see
    lichess_sync's lichess_round_map table) and only re-fetched when a round
    number we don't have cached yet is needed.
    """
    resp = _get(f"/api/broadcast/{broadcast_id}")
    data = resp.json()
    out = []
    for r in data.get("rounds", []):
        name = r.get("name", "")
        try:
            number = int(name.strip().rsplit(" ", 1)[-1])
        except ValueError:
            continue  # not a plain "Round N" (rare non-standard round name) -- skip
        out.append(BroadcastRound(id=r["id"], number=number, finished=bool(r.get("finished"))))
    return out


def _parse_pgn_tag(line: str) -> Optional[tuple[str, str]]:
    if not (line.startswith("[") and line.endswith("]")):
        return None
    line = line[1:-1]
    key, _, rest = line.partition(" ")
    value = rest.strip().strip('"')
    return key, value


def fetch_round_games(round_id: str) -> list[dict]:
    """Every game of one broadcast round, as row dicts ready to match against
    our own `teams`/`players` tables (see lichess_sync.py). No clocks/eval
    comments requested -- we only need the result and header tags, and a
    lighter response is politer to ask for repeatedly.
    """
    resp = _get(f"/api/broadcast/round/{round_id}.pgn", clocks="false", comments="false")
    games = []
    current: dict[str, str] = {}
    for raw_line in resp.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tag = _parse_pgn_tag(line)
        if tag is None:
            continue  # movetext line -- header-only parsing, no need to read moves
        key, value = tag
        if key == "Event" and current:
            games.append(current)
            current = {}
        current[key] = value
    if current:
        games.append(current)

    rows = []
    for g in games:
        round_no_str = g.get("Round", "")
        try:
            round_no = int(round_no_str.split(".")[0])
        except ValueError:
            continue
        result = g.get("Result", "*")
        rows.append(
            {
                "round": round_no,
                "white_name": g.get("White", ""),
                "black_name": g.get("Black", ""),
                "white_team_name": g.get("WhiteTeam", ""),
                "black_team_name": g.get("BlackTeam", ""),
                "white_fide_id": int(g["WhiteFideId"]) if g.get("WhiteFideId", "").isdigit() else None,
                "black_fide_id": int(g["BlackFideId"]) if g.get("BlackFideId", "").isdigit() else None,
                "white_rating": int(g["WhiteElo"]) if g.get("WhiteElo", "").isdigit() else None,
                "black_rating": int(g["BlackElo"]) if g.get("BlackElo", "").isdigit() else None,
                "result": result if result in ("1-0", "0-1", "1/2-1/2") else None,
            }
        )
    return rows
