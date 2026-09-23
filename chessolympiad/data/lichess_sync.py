"""Sync board-level round results from lichess.org broadcasts into the same
`games`/`matches` tables chess-results.com sync populates (chessolympiad.data.sync)
-- everything downstream (matches derivation, standings, forecasts, exports)
reads those tables and needs no changes to work from either source.

Why Lichess is primary for the live 2026 event: see lichess_client.py's
module docstring. chess-results.com remains the source for team lists and
rosters (chessolympiad.data.sync.sync_tournament with fetch_round_results=False)
-- Lichess broadcasts don't carry seed/initial-rank or full reserve rosters.
"""

from __future__ import annotations

import re
import time
import unicodedata

from chessolympiad.data import db
from chessolympiad.data import lichess_client as lc
from chessolympiad.data.sync import derive_matches


def _normalize_team_name(name: str) -> str:
    """Canonical form for matching a Lichess team name against our own
    chess-results.com-sourced team_name -- the two sources agree on every
    team's name except a couple of "X and Y" vs "X & Y" federations, plus
    the occasional curly vs. straight apostrophe (verified across all 395
    teams in both 2026 sections; see the reconciliation this was built
    from). NFKD + stripping diacritics guards against any future accent
    mismatch (e.g. a "Turkiye"/"Türkiye" spelling difference) neither
    source happens to have today.
    """
    name = name.replace("’", "'").replace("&", "and")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def _team_lookup(conn, tournament_id: str) -> dict[str, int]:
    return {
        _normalize_team_name(row["team_name"]): row["team_no"]
        for row in conn.execute(
            "SELECT team_no, team_name FROM teams WHERE tournament_id = ?", (tournament_id,)
        ).fetchall()
    }


def _round_map(conn, section: str) -> dict[str, tuple[str, int, bool]]:
    """round_id -> (broadcast_id, round_number, finished), across every
    sub-broadcast for this section, from our local cache."""
    out = {}
    for row in conn.execute(
        "SELECT broadcast_id, round_id, round_number, finished FROM lichess_round_map WHERE broadcast_id IN ({})".format(
            ", ".join("?" * len(lc.BROADCAST_IDS[section]))
        ),
        lc.BROADCAST_IDS[section],
    ).fetchall():
        out[row["round_id"]] = (row["broadcast_id"], row["round_number"], bool(row["finished"]))
    return out


def _refresh_round_map(conn, section: str, progress=None) -> None:
    """Re-fetch every sub-broadcast's round list for this section (a few KB
    each) and overwrite the cache. Only called when a round we need isn't
    cached yet for every sub-broadcast (see _needs_round_map_refresh) --
    steady state, once a round is cached everywhere, costs nothing.
    """
    rows = []
    for broadcast_id in lc.BROADCAST_IDS[section]:
        for r in lc.fetch_broadcast_rounds(broadcast_id):
            rows.append(
                {
                    "broadcast_id": broadcast_id,
                    "round_id": r.id,
                    "round_number": r.number,
                    "finished": int(r.finished),
                }
            )
    db.upsert(conn, "lichess_round_map", rows, key_cols=["broadcast_id", "round_id"])
    if progress:
        progress(f"lichess: refreshed round map for {section} ({len(rows)} rounds across {len(lc.BROADCAST_IDS[section])} broadcasts)")


def _needs_round_map_refresh(conn, section: str, round_no: int) -> bool:
    """Whether we're missing round_no's ID for any of this section's
    sub-broadcasts. Round IDs are permanent once created, so once every
    sub-broadcast has round_no cached this returns False forever for that
    round -- completeness (whether it's safe to stop re-fetching the PGN)
    is decided separately, from our own `games` table, not from Lichess's
    "finished" flag.
    """
    cached = _round_map(conn, section)
    known_rounds_per_broadcast: dict[str, set[int]] = {}
    for _rid, (bid, num, _finished) in cached.items():
        known_rounds_per_broadcast.setdefault(bid, set()).add(num)
    return any(round_no not in known_rounds_per_broadcast.get(bid, set()) for bid in lc.BROADCAST_IDS[section])


def _group_into_games_rows(
    raw_games: list[dict], teams: dict[str, int], tournament_id: str, round_no: int
) -> list[dict]:
    """Consecutive games sharing the same (unordered) team pair are one
    match; position within that run is the board number -- verified
    directly against a live round while building this (boards for a match
    are always contiguous in the broadcast PGN, in ascending board order).
    Unmatched team names are skipped rather than crashing the sync (see
    _normalize_team_name's docstring -- shouldn't happen, all 395 teams
    across both 2026 sections reconcile cleanly).
    """
    rows: list[dict] = []

    def flush(group: list[dict]) -> None:
        if not group:
            return
        a_name, b_name = group[0]["white_team_name"], group[0]["black_team_name"]
        team_a_no = teams.get(_normalize_team_name(a_name))
        team_b_no = teams.get(_normalize_team_name(b_name))
        if team_a_no is None or team_b_no is None:
            return
        for board_no, g in enumerate(group, start=1):
            white_team_no = team_a_no if g["white_team_name"] == a_name else team_b_no
            rows.append(
                {
                    "tournament_id": tournament_id,
                    "round": round_no,
                    "team_a_no": team_a_no,
                    "team_b_no": team_b_no,
                    "board_no": board_no,
                    "white_team_no": white_team_no,
                    "white_fide_id": g["white_fide_id"],
                    "white_name": g["white_name"],
                    "white_rating": g["white_rating"],
                    "black_fide_id": g["black_fide_id"],
                    "black_name": g["black_name"],
                    "black_rating": g["black_rating"],
                    "result": g["result"],
                    "forfeit": 0,
                }
            )

    group_key = None
    group: list[dict] = []
    for g in raw_games:
        key = frozenset((g["white_team_name"], g["black_team_name"]))
        if key != group_key:
            flush(group)
            group, group_key = [], key
        group.append(g)
    flush(group)
    return rows


# A live-cadence lichess sync should never legitimately need this long --
# caps the whole tournament sync's worst case regardless of how many
# rounds/sub-broadcasts are in play or how slow any single request is.
# This is defense in depth, not the primary fix: the primary fix is not
# re-probing an abandoned round at all (see sync_tournament_from_lichess).
SYNC_BUDGET_SECONDS = 45


def sync_round_from_lichess(conn, tournament_id: str, section: str, round_no: int, progress=None, deadline: float | None = None) -> int:
    """Sync one round's board results from every sub-broadcast covering
    this section. Returns the number of boards written (0 if nothing new
    -- either no sub-broadcast has reached this round yet, or it's already
    fully decided locally and skipped via the same _complete_rounds check
    chess-results-sourced rounds use).

    `deadline` (a time.monotonic() timestamp) stops issuing further
    fetch_round_games calls once passed, returning whatever was gathered
    so far -- a round can span several sub-broadcasts, and this is what
    actually bounds a single round's worst-case time when the deadline is
    reached mid-round rather than only checked between rounds.
    """
    from chessolympiad.data.sync import _complete_rounds  # local import: avoid a cycle at module load

    if round_no in _complete_rounds(conn, tournament_id):
        return 0  # already fully decided locally (from either source) -- can't change

    if _needs_round_map_refresh(conn, section, round_no):
        _refresh_round_map(conn, section, progress=progress)

    round_map = _round_map(conn, section)
    teams = _team_lookup(conn, tournament_id)

    game_rows: list[dict] = []
    for round_id, (_broadcast_id, num, _finished) in round_map.items():
        if num != round_no:
            continue
        if deadline is not None and time.monotonic() > deadline:
            if progress:
                progress(f"{tournament_id}: lichess sync budget exceeded mid-round {round_no}, stopping early this cycle")
            break
        raw_games = lc.fetch_round_games(round_id)
        game_rows.extend(_group_into_games_rows(raw_games, teams, tournament_id, round_no))

    if not game_rows:
        return 0

    conn.execute("DELETE FROM games WHERE tournament_id = ? AND round = ?", (tournament_id, round_no))
    conn.execute("DELETE FROM matches WHERE tournament_id = ? AND round = ?", (tournament_id, round_no))
    db.upsert(conn, "games", game_rows, key_cols=["tournament_id", "round", "team_a_no", "team_b_no", "board_no"])
    derive_matches(conn, tournament_id, [round_no])
    if progress:
        progress(f"{tournament_id}: round {round_no} synced from lichess ({len(game_rows)} boards)")
    return len(game_rows)


def sync_tournament_from_lichess(conn, tournament_id: str, section: str, num_rounds: int, progress=None) -> int:
    """Sync every not-yet-complete round AFTER the highest already-complete
    one, stopping at the first round no sub-broadcast has reached yet
    (mirrors sync.py's own round loop) -- a not-yet-known round is always
    probed regardless of the official schedule, same reasoning as sync.py's
    module docstring.

    Deliberately NOT "every incomplete round from 1": a round can have one
    permanently-missing board (a walkover/forfeit lichess never gets a
    result for) long after the tournament has moved past it. Re-probing
    that forever wastes a full round's worth of requests (up to 5
    sub-broadcasts) on every single call for no possible new data -- this
    was a real, ongoing contributor to hitting lichess's rate limit and to
    a sync call's worst-case duration. Once a later round has completed,
    an earlier incomplete one is abandoned, not in progress.
    """
    from chessolympiad.data.sync import _complete_rounds  # local import: avoid a cycle at module load

    complete_rounds = _complete_rounds(conn, tournament_id)
    max_complete = max(complete_rounds, default=0)
    deadline = time.monotonic() + SYNC_BUDGET_SECONDS
    total = 0
    for round_no in range(max_complete + 1, num_rounds + 1):
        if round_no in complete_rounds:
            continue  # can't change -- but later rounds might still have new data, keep going
        if time.monotonic() > deadline:
            if progress:
                progress(f"{tournament_id}: lichess sync budget ({SYNC_BUDGET_SECONDS}s) exceeded, stopping early this cycle")
            break
        written = sync_round_from_lichess(conn, tournament_id, section, round_no, progress=progress, deadline=deadline)
        total += written
        if written == 0:
            break  # no sub-broadcast has reached this round yet
    return total
