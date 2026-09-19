"""Idempotent resync of one tournament's data from chess-results.com into
the local SQLite mirror.

A recurring/live call is deliberately *not* a blind full re-fetch -- two
layers keep its chess-results.com request volume down to just whatever
could plausibly have changed since the last call (see the incident this
was built to fix: a naive full-refetch-every-cycle design got this
project's IP rate-limited and blocked):

  1. Rosters (fetch_rosters) and a re-check of already-decided rounds
     (refetch_complete_rounds) default to off for a recurring call --
     rosters rarely change mid-event and a fully-decided round's result
     can't change short of a rare post-hoc correction. Both are still
     exercised periodically via an occasional full-refresh call.
  2. A round already recorded locally as fully decided is never
     re-fetched at all regardless of the above flags (see
     `_complete_rounds`) -- otherwise a recurring job's per-cycle request
     count would grow by one round's worth of requests every time a new
     round finished.

A round not yet known locally is *always* probed regardless of its
official start time: chess-results.com routinely publishes a round's
pairings hours (sometimes the day) before it actually starts, and an
earlier version of this gate blocked exactly that (round 4's pairings
went unnoticed for hours because the round hadn't officially started
yet). That probe is one cheap request per cycle in the worst case (an
empty result -- see the `break` below), so there's no real volume cost
to just always trying.

Net effect for the live 2026 event: a routine call only hits
chess-results.com for the team list, whatever round is currently in
progress, and one extra check for the next round's pairings -- and
stops touching a round entirely once it's complete.
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


def _complete_rounds(conn, tournament_id: str) -> set[int]:
    """Rounds already in `games` where every board has a result. These
    can't change (short of a rare post-hoc arbiter correction, caught by
    the periodic `refetch_complete_rounds=True` pass instead) so a live
    sync doesn't need to re-fetch them every cycle -- without this, a
    recurring job's per-cycle request count grows by one round's worth of
    requests every time a new round finishes, eventually exceeding
    chess-results.com's daily cap on its own even with rosters excluded.
    """
    cur = conn.execute(
        "SELECT round FROM games WHERE tournament_id = ? "
        "GROUP BY round HAVING SUM(CASE WHEN result IS NULL OR result = '' THEN 1 ELSE 0 END) = 0",
        (tournament_id,),
    )
    return {row["round"] for row in cur.fetchall()}


def sync_tournament(
    tournament_id: str,
    tnr: int,
    section: str,
    year: int,
    max_round_probe: int | None = None,
    progress=None,
    fetch_rosters: bool = True,
    refetch_complete_rounds: bool = True,
    fetch_round_results: bool = True,
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

    refetch_complete_rounds: when False, rounds already fully decided in
    our own DB are left as-is instead of being re-fetched -- only rounds
    still in progress (or not yet seen) hit chess-results.com. Pass False
    for frequent/recurring syncs and True for an occasional full refresh
    (a live tournament round can rarely get a post-hoc correction).

    fetch_round_results: skip board-level round results entirely --
    chessolympiad.data.lichess_sync is the primary source for these on the
    live 2026 event (faster, and unlike chess-results.com has no daily
    request cap), so the recurring live-update call only still needs this
    for the team list + rosters. Historical/manual full-sync calls keep
    this on since there's no Lichess broadcast to fall back on for them.
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

        # chess-results.com's team_no is a starting-rank *position*: a
        # mid-event withdrawal shifts every later team's number down by one,
        # so a team_no that no longer appears in the current fetch isn't
        # just "gone" -- it may now belong to a different team entirely (the
        # one that shifted into it), and its old roster is stale regardless.
        # Garbage-collect it rather than let an upsert-only sync accumulate
        # a phantom extra team forever (this is what was inflating numTeams
        # and, more seriously, letting one real team's round results get
        # recorded under two different team_no values across a renumbering).
        current_nos = [t["team_no"] for t in meta.teams]
        if current_nos:
            placeholders = ", ".join("?" * len(current_nos))
            conn.execute(
                f"DELETE FROM teams WHERE tournament_id = ? AND team_no NOT IN ({placeholders})",
                [tournament_id, *current_nos],
            )
            conn.execute(
                f"DELETE FROM players WHERE tournament_id = ? AND team_no NOT IN ({placeholders})",
                [tournament_id, *current_nos],
            )

        if fetch_rosters:
            if progress:
                progress(f"{tournament_id}: {len(team_rows)} teams synced, fetching rosters...")
            player_rows = []
            for t in meta.teams:
                roster = cr.fetch_team_roster(tnr, t["team_no"])
                for p in roster:
                    player_rows.append({**p, "tournament_id": tournament_id, "team_no": t["team_no"]})
            db.upsert(conn, "players", player_rows, key_cols=["tournament_id", "team_no", "board_no"])

        # Sourced from the `players` table (not just rosters fetched in
        # *this* call) so that white/black_fide_id linkage still works on a
        # recurring sync that skips re-fetching rosters -- the mapping from
        # an earlier call's roster fetch is already sitting in the DB.
        name_to_fide: dict[tuple[str, int, str], int] = {}
        for row in conn.execute(
            "SELECT team_no, name, fide_id FROM players WHERE tournament_id = ? AND fide_id IS NOT NULL",
            (tournament_id,),
        ).fetchall():
            key = (tournament_id, row["team_no"], _normalize_name(row["name"]))
            name_to_fide[key] = row["fide_id"]

        rounds_synced = []
        total_games = 0
        if fetch_round_results:
            complete_rounds = set() if refetch_complete_rounds else _complete_rounds(conn, tournament_id)
            rounds_to_try = range(1, (max_round_probe or meta.num_rounds) + 1)
            for rd in rounds_to_try:
                if rd in complete_rounds:
                    rounds_synced.append(rd)  # already fully decided locally, no need to re-fetch
                    continue
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
                # Delete-then-insert, not upsert-and-accumulate: chess-results.com's
                # team_no is a starting-rank *position*, not a permanent ID -- a
                # mid-event withdrawal shifts every later team's number down by
                # one. Upserting by (round, team_a_no, team_b_no, board_no) would
                # then leave the pre-shift rows sitting alongside the post-shift
                # ones forever (same real match, two different team_no pairs),
                # which is exactly what showed up as one team appearing twice in
                # a single round's pairings. A full re-fetch of this round is
                # already happening every sync regardless, so replacing its
                # rows outright is always safe and never loses data.
                conn.execute("DELETE FROM games WHERE tournament_id = ? AND round = ?", (tournament_id, rd))
                conn.execute("DELETE FROM matches WHERE tournament_id = ? AND round = ?", (tournament_id, rd))
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
                derive_matches(conn, tournament_id, rounds_synced)

        return SyncResult(
            tournament_id=tournament_id,
            num_teams=len(team_rows),
            rounds_synced=rounds_synced,
            total_games=total_games,
        )
    finally:
        conn.close()


def derive_matches(conn, tournament_id: str, rounds: list[int]) -> None:
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
