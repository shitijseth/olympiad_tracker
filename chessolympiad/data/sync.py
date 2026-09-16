"""Idempotent full-resync of one tournament's data from chess-results.com
into the local SQLite mirror.

Every call re-fetches current rosters and all available round results and
upserts them -- never appends -- so the DB always mirrors whatever
chess-results.com currently shows (rosters and even ratings can change
right up to Round 1, and captains can substitute players between rounds
during the event).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from chessolympiad.data import chessresults_client as cr
from chessolympiad.data import db


def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[,\.]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return " ".join(sorted(name.split()))  # order-insensitive: "Last, First" == "First Last"


@dataclass
class SyncResult:
    tournament_id: str
    num_teams: int
    rounds_synced: list[int]
    total_games: int


def sync_tournament(
    tournament_id: str,
    tnr: int,
    section: str,
    year: int,
    max_round_probe: int | None = None,
    progress=None,
    fetch_rosters: bool = True,
) -> SyncResult:
    """Full resync: tournament metadata, teams, rosters, and every round of
    board-level results that currently has data.

    max_round_probe: how many rounds to *attempt* fetching (defaults to the
    tournament's declared num_rounds). Rounds with zero board rows returned
    are treated as "not played yet" and stop the round loop for a live
    tournament -- but for a finished historical one every round will return
    data, so this naturally ingests the whole event in one call.

    fetch_rosters: skip roster ingestion for historical events used only
    for calibration (game-level ratings already come from `games`), to
    avoid hundreds of extra requests per tournament.
    """
    conn = db.connect()
    try:
        meta = cr.fetch_teams(tnr)
        db.upsert(
            conn,
            "tournaments",
            [
                {
                    "tournament_id": tournament_id,
                    "tnr": tnr,
                    "section": section,
                    "year": year,
                    "name": meta.name,
                    "num_rounds": meta.num_rounds,
                    "fide_event_id": meta.fide_event_id,
                    "last_synced_at": dt.datetime.utcnow().isoformat(),
                }
            ],
            key_cols=["tournament_id"],
        )

        team_rows = [{**t, "tournament_id": tournament_id} for t in meta.teams]
        db.upsert(conn, "teams", team_rows, key_cols=["tournament_id", "team_no"])

        name_to_fide: dict[tuple[str, int, str], int] = {}
        if fetch_rosters:
            if progress:
                progress(f"{tournament_id}: {len(team_rows)} teams synced, fetching rosters...")
            player_rows = []
            for t in meta.teams:
                roster = cr.fetch_team_roster(tnr, t["team_no"])
                for p in roster:
                    player_rows.append({**p, "tournament_id": tournament_id, "team_no": t["team_no"]})
            db.upsert(conn, "players", player_rows, key_cols=["tournament_id", "team_no", "board_no"])
            for p in player_rows:
                if p.get("fide_id"):
                    key = (tournament_id, p["team_no"], _normalize_name(p["name"]))
                    name_to_fide[key] = p["fide_id"]

        rounds_to_try = range(1, (max_round_probe or meta.num_rounds) + 1)
        rounds_synced = []
        total_games = 0
        for rd in rounds_to_try:
            games = cr.fetch_round_board_results(tnr, rd)
            if not games:
                break  # this round hasn't been played/published yet
            game_rows = []
            for g in games:
                row = {**g, "tournament_id": tournament_id}
                row["white_fide_id"] = name_to_fide.get(
                    (tournament_id, row["white_team_no"], _normalize_name(row["white_name"]))
                )
                black_team_no = row["team_b_no"] if row["white_team_no"] == row["team_a_no"] else row["team_a_no"]
                row["black_fide_id"] = name_to_fide.get(
                    (tournament_id, black_team_no, _normalize_name(row["black_name"]))
                )
                row["forfeit"] = int(row["forfeit"])
                game_rows.append(row)
            db.upsert(
                conn,
                "games",
                game_rows,
                key_cols=["tournament_id", "round", "team_a_no", "team_b_no", "board_no"],
            )
            rounds_synced.append(rd)
            total_games += len(game_rows)
            if progress:
                progress(f"{tournament_id}: round {rd} synced ({len(game_rows)} boards)")

        if rounds_synced:
            _derive_matches(conn, tournament_id, rounds_synced)

        return SyncResult(
            tournament_id=tournament_id,
            num_teams=len(team_rows),
            rounds_synced=rounds_synced,
            total_games=total_games,
        )
    finally:
        conn.close()


def _derive_matches(conn, tournament_id: str, rounds: list[int]) -> None:
    """Aggregate the games fact table into team-level match points per
    Article 4.9.1: >2 game points wins the match (2 MP), =2 draws (1-1),
    <2 loses (0 MP) -- computed straight from real board results, not
    re-fetched from a separate view.
    """
    placeholders = ", ".join("?" * len(rounds))
    cur = conn.execute(
        f"SELECT round, team_a_no, team_b_no, board_no, white_team_no, result, forfeit "
        f"FROM games WHERE tournament_id = ? AND round IN ({placeholders})",
        [tournament_id, *rounds],
    )
    agg: dict[tuple[int, int, int], list[float]] = {}
    total_boards: dict[tuple[int, int, int], int] = {}
    reported_boards: dict[tuple[int, int, int], int] = {}
    for row in cur.fetchall():
        key = (row["round"], row["team_a_no"], row["team_b_no"])
        a_pts, b_pts = agg.get(key, [0.0, 0.0])
        total_boards[key] = total_boards.get(key, 0) + 1
        if row["result"] == "1-0":
            w, b = 1.0, 0.0
        elif row["result"] == "0-1":
            w, b = 0.0, 1.0
        elif row["result"] == "1/2-1/2":
            w, b = 0.5, 0.5
        else:
            w = b = None
        if w is not None:
            reported_boards[key] = reported_boards.get(key, 0) + 1
            if row["white_team_no"] == row["team_a_no"]:
                a_pts += w
                b_pts += b
            else:
                a_pts += b
                b_pts += w
        agg[key] = [a_pts, b_pts]

    # A live dashboard should show each match's result as soon as THAT
    # match is decided, not wait for the whole round -- one adjourned or
    # slow-finishing board out of ~100 pairings would otherwise hide every
    # already-final result for hours. So the gate here is per-MATCH: every
    # board in *this* pairing must have reported a result (still guarding
    # against the earlier bug of scoring a still-open board as 0-0), but
    # different matches in the same round populate independently as each
    # finishes. asOfRound (computed from whatever rounds have at least one
    # match row) is therefore a "round N is under way" signal, not "round N
    # is fully over" -- real standings/ranks built from it are genuinely
    # live and can still shift as the remaining matches in the round finish.
    match_rows = []
    for (rd, a, b), (a_pts, b_pts) in agg.items():
        key = (rd, a, b)
        if reported_boards.get(key, 0) < total_boards[key]:
            continue
        if a_pts > b_pts:
            a_mp, b_mp = 2, 0
        elif a_pts < b_pts:
            a_mp, b_mp = 0, 2
        else:
            a_mp, b_mp = 1, 1
        match_rows.append(
            {
                "tournament_id": tournament_id,
                "round": rd,
                "team_a_no": a,
                "team_b_no": b,
                "team_a_game_pts": a_pts,
                "team_b_game_pts": b_pts,
                "team_a_match_pts": a_mp,
                "team_b_match_pts": b_mp,
                "is_bye": 0,
            }
        )
    db.upsert(conn, "matches", match_rows, key_cols=["tournament_id", "round", "team_a_no", "team_b_no"])
