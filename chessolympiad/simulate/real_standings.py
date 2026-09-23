"""Derive current real (not simulated) standings, per-team round-by-round
result history, and per-player real performance stats from whatever real
rounds have been ingested into `games`/`matches` so far -- this is the data
the live dashboard needs to show actual tournament progress alongside the
Monte Carlo forecast, once the event is underway.

`compute_real_snapshot` deliberately reuses simulate.round.apply_real_round
-- the exact same code path run_monte_carlo uses to ingest real rounds
before simulating the remainder -- rather than re-deriving match points/
tiebreak history independently, so "current real standings" can never
drift out of sync with how the live forecast itself interprets those rounds
(byes included: apply_real_round's is_bye handling is authoritative).
"""

from __future__ import annotations

import sqlite3

from chessolympiad.pairing.swiss_team import TeamPairingState
from chessolympiad.simulate.round import TournamentState, apply_real_round
from chessolympiad.simulate.tiebreak import compute_tiebreaks, rank_teams
from chessolympiad.simulate.tournament import RealRoundData, TeamInfo


def compute_real_snapshot(
    teams: list[TeamInfo],
    rosters: dict[int, list[dict]],
    real_rounds: dict[int, RealRoundData],
    num_rounds: int,
) -> tuple[TournamentState, dict[int, dict]]:
    """Replays every real round ingested so far (in round order) into a
    fresh TournamentState, then returns (state, standings) where standings
    is {team_no: {"mp": int, "rank": int, "tb": (tb1, tb2, tb3)}}.

    `num_rounds` is the TOURNAMENT'S total declared round count (e.g. 11),
    not just the number of real rounds ingested -- Annex 2.I's bye/unplayed
    substitute formulas need "rounds remaining after this one" relative to
    the whole event, which only equals "real rounds ingested so far" once
    the event has actually finished.
    """
    pairing_states = {t.team_no: TeamPairingState(team_no=t.team_no, seed=t.initial_rank) for t in teams}
    team_ratings = {t.team_no: t.rating_avg for t in teams}
    state = TournamentState(pairing_states=pairing_states, rosters=rosters, team_ratings=team_ratings)

    for rd in sorted(real_rounds.keys()):
        remaining_after = num_rounds - rd
        apply_real_round(state, real_rounds[rd].matches, real_rounds[rd].games, remaining_after)

    if not real_rounds:
        return state, {}

    tiebreaks = compute_tiebreaks(state.history, state.match_points)
    ranking = rank_teams(state.match_points, tiebreaks)
    rank_pos = {team_no: i + 1 for i, team_no in enumerate(ranking)}
    standings = {
        team_no: {"mp": state.match_points[team_no], "rank": rank_pos[team_no], "tb": list(tiebreaks[team_no])}
        for team_no in state.match_points
    }
    return state, standings


def real_round_history(conn: sqlite3.Connection, tournament_id: str, team_names: dict[int, dict]) -> dict[int, list[dict]]:
    """Per-team, round-by-round real results for display (Team detail):
    {team_no: [{"round", "opponentNo", "opponentFed", "opponentName",
    "ownGamePts", "oppGamePts", "ownMatchPts", "result", "isBye",
    "boards": [...]}]}. `team_names` is {team_no: {"fed":, "name":}}, used
    to label the opponent without a second query per round.
    """
    rounds = [
        r["round"]
        for r in conn.execute(
            "SELECT DISTINCT round FROM matches WHERE tournament_id = ? ORDER BY round", (tournament_id,)
        ).fetchall()
    ]
    out: dict[int, list[dict]] = {}
    for rd in rounds:
        matches = conn.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND round = ?", (tournament_id, rd)
        ).fetchall()
        games_by_pair: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for g in conn.execute(
            "SELECT * FROM games WHERE tournament_id = ? AND round = ? ORDER BY board_no", (tournament_id, rd)
        ).fetchall():
            games_by_pair.setdefault((g["team_a_no"], g["team_b_no"]), []).append(g)

        for m in matches:
            for own, opp, own_pts, opp_pts, own_mp in (
                (m["team_a_no"], m["team_b_no"], m["team_a_game_pts"], m["team_b_game_pts"], m["team_a_match_pts"]),
                (m["team_b_no"], m["team_a_no"], m["team_b_game_pts"], m["team_a_game_pts"], m["team_b_match_pts"]),
            ):
                if own is None or not own:
                    continue
                is_bye = bool(m["is_bye"]) or not opp
                if is_bye and own != m["team_a_no"]:
                    continue  # a bye row's "team_b" side (NULL/0) has nothing to report
                result = "bye" if is_bye else ("W" if own_mp == 2 else "D" if own_mp == 1 else "L")
                boards = []
                if not is_bye:
                    pair_games = games_by_pair.get((m["team_a_no"], m["team_b_no"]), [])
                    for g in pair_games:
                        own_is_white = g["white_team_no"] == own
                        boards.append({
                            "boardNo": g["board_no"],
                            "ownName": g["white_name"] if own_is_white else g["black_name"],
                            "ownRating": g["white_rating"] if own_is_white else g["black_rating"],
                            "oppName": g["black_name"] if own_is_white else g["white_name"],
                            "oppRating": g["black_rating"] if own_is_white else g["white_rating"],
                            "ownIsWhite": own_is_white,
                            "result": g["result"],
                        })
                opp_info = team_names.get(opp, {}) if opp else {}
                out.setdefault(own, []).append({
                    "round": rd,
                    "opponentNo": opp if opp else None,
                    "opponentFed": opp_info.get("fed"),
                    "opponentName": opp_info.get("name"),
                    "ownGamePts": own_pts,
                    "oppGamePts": opp_pts,
                    "ownMatchPts": own_mp,
                    "result": result,
                    "isBye": is_bye,
                    "boards": boards,
                })
    return out


def real_rounds_for_replay(conn: sqlite3.Connection, tournament_id: str) -> dict[int, list[dict]]:
    """Compact real-round data for the dashboard's client-side simulator to
    replay exactly -- real pairings/results aren't reproducible by the
    synthetic Swiss-pairing algorithm, so the Simulate tab must ingest them
    verbatim (mirroring apply_real_round) before it starts generating
    rounds asOfRound+1 onward itself. Keyed by round number.

    Unlike `matches` itself (which populates per-match as each finishes, so
    the live leaderboard/board medals can show results as they land), the
    Simulate tab needs a WHOLE round before replaying it: pairing-history
    bookkeeping (no-rematch, colour balance, byes) has to advance every
    team together, or the handful of teams still mid-match would silently
    get skipped this round and re-paired incorrectly once synthetic
    rounds resume. So a round is only included here once every published
    game in it has a result (checked against `games`, not team coverage in
    `matches` -- a withdrawn/no-show team never gets a pairing again for
    the rest of the tournament, so requiring every originally-registered
    team to appear every round would make this permanently empty once
    that happens). real_standings and the live tables read `matches`
    directly and are unaffected by any of this.
    """
    rounds = [
        r["round"]
        for r in conn.execute(
            "SELECT DISTINCT round FROM matches WHERE tournament_id = ? ORDER BY round", (tournament_id,)
        ).fetchall()
    ]
    if rounds:
        rounds = [
            r["round"]
            for r in conn.execute(
                """
                SELECT round FROM games WHERE tournament_id = ? AND round IN ({})
                GROUP BY round
                HAVING SUM(CASE WHEN result IS NULL OR result = '' THEN 1 ELSE 0 END) = 0
                ORDER BY round
                """.format(", ".join("?" * len(rounds))),
                (tournament_id, *rounds),
            ).fetchall()
        ]
    out: dict[int, list[dict]] = {}
    for rd in rounds:
        matches = conn.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND round = ?", (tournament_id, rd)
        ).fetchall()
        games_by_pair: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for g in conn.execute(
            "SELECT * FROM games WHERE tournament_id = ? AND round = ? ORDER BY board_no", (tournament_id, rd)
        ).fetchall():
            games_by_pair.setdefault((g["team_a_no"], g["team_b_no"]), []).append(g)

        round_out = []
        for m in matches:
            a, b = m["team_a_no"], m["team_b_no"]
            if m["is_bye"] or not b:
                round_out.append({"teamA": a, "teamB": None, "isBye": True})
                continue
            games = games_by_pair.get((a, b), [])
            board1 = next((g for g in games if g["board_no"] == 1), None)
            team_a_white_odd = (board1["white_team_no"] == a) if board1 else True
            round_out.append({
                "teamA": a, "teamB": b, "isBye": False,
                "teamAGamePts": m["team_a_game_pts"], "teamBGamePts": m["team_b_game_pts"],
                "teamAMatchPts": m["team_a_match_pts"], "teamBMatchPts": m["team_b_match_pts"],
                "teamAWhiteOdd": team_a_white_odd,
                "boards": [
                    {
                        "boardNo": g["board_no"], "whiteTeam": g["white_team_no"],
                        "whiteName": g["white_name"], "whiteRating": g["white_rating"],
                        "blackName": g["black_name"], "blackRating": g["black_rating"],
                        "result": g["result"],
                    }
                    for g in games
                ],
            })
        out[rd] = round_out
    return out


def real_pairings(conn: sqlite3.Connection, tournament_id: str) -> dict[int, list[dict]]:
    """Published board pairings per round, straight from `games`, regardless
    of whether any result has been reported yet -- chess-results.com posts
    each round's board assignments (names, ratings, boards) before a single
    game finishes, and the Rounds display page should show "who's playing
    whom" as soon as that's out, board results filling in as they land.

    Deliberately NOT sourced from `matches`/real_rounds_for_replay: that
    table only ever gets a row for a pairing once every board in THAT
    match has reported a result (see _derive_matches), which is exactly
    what keeps a pairings-only match from being mistaken for a played one
    everywhere else (asOfRound, the Simulate tab's replay, real
    standings). This function is for display only and must never feed
    those.
    """
    rounds = [
        r["round"]
        for r in conn.execute(
            "SELECT DISTINCT round FROM games WHERE tournament_id = ? ORDER BY round", (tournament_id,)
        ).fetchall()
    ]
    out: dict[int, list[dict]] = {}
    for rd in rounds:
        games = conn.execute(
            "SELECT * FROM games WHERE tournament_id = ? AND round = ? ORDER BY team_a_no, team_b_no, board_no",
            (tournament_id, rd),
        ).fetchall()
        by_pair: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for g in games:
            by_pair.setdefault((g["team_a_no"], g["team_b_no"]), []).append(g)

        round_out = []
        for (a, b), gs in by_pair.items():
            board1 = next((g for g in gs if g["board_no"] == 1), None)
            team_a_white_odd = (board1["white_team_no"] == a) if board1 else True
            a_pts = b_pts = 0.0
            any_result = False
            for g in gs:
                if g["result"] == "1-0":
                    w, l = 1.0, 0.0
                elif g["result"] == "0-1":
                    w, l = 0.0, 1.0
                elif g["result"] == "1/2-1/2":
                    w, l = 0.5, 0.5
                else:
                    continue
                any_result = True
                if g["white_team_no"] == a:
                    a_pts, b_pts = a_pts + w, b_pts + l
                else:
                    a_pts, b_pts = a_pts + l, b_pts + w
            round_out.append({
                "teamA": a, "teamB": b,
                "hasResults": any_result,
                "teamAGamePts": a_pts if any_result else None,
                "teamBGamePts": b_pts if any_result else None,
                "teamAWhiteOdd": team_a_white_odd,
                "boards": [
                    {
                        "boardNo": g["board_no"], "whiteTeam": g["white_team_no"],
                        "whiteName": g["white_name"], "whiteRating": g["white_rating"],
                        "blackName": g["black_name"], "blackRating": g["black_rating"],
                        "result": g["result"],
                    }
                    for g in gs
                ],
            })
        out[rd] = round_out
    return out


def real_player_stats(state: TournamentState) -> dict[tuple[int, int, str], dict]:
    """Real (not simulated) per-player games/score/TPR-so-far, keyed the
    same way as simulate.round.PlayerStat -- (team_no, board_no,
    player_key) -- so export code can merge these into the simulated
    per-board forecast entries by the same key.
    """
    out: dict[tuple[int, int, str], dict] = {}
    for key, stat in state.player_stats.items():
        tpr = None
        if stat.games > 0:
            tpr = stat.opp_rating_sum / stat.games + 800.0 * (stat.score / stat.games - 0.5)
        out[key] = {
            "teamNo": stat.team_no,
            "boardNo": stat.board_no,
            "fideId": stat.fide_id,
            "name": stat.name,
            "games": stat.games,
            "score": stat.score,
            "tpr": round(tpr) if tpr is not None else None,
        }
    return out


def real_board_standings(
    real_pstats: dict[tuple[int, int, str], dict], team_names: dict[int, dict]
) -> dict[int, list[dict]]:
    """Current real (not simulated) board-medal standings, per board_no
    (1-4, plus 5 for the reserve/"best reserve" prize) -- FIDE Chess
    Olympiad 2026 Regulations Article 4.6.3 + Appendix 2.III (see
    reference/fide_olympiad_2026_regulations.md): ranked by performance
    rating (TPR), ties broken by games played, eligibility requires at
    least 8 games played. See chessolympiad.simulate.board_tiebreak for
    the ranking rule itself.

    Returns {board_no: [{"teamNo", "fed", "team", "fideId", "name",
    "games", "tpr", "eligible", "rank"}, ...]}, eligible players first in
    rank order, then ineligible players (rank=None) -- callers that only
    want current medal contenders should filter on "eligible".
    """
    from chessolympiad.simulate.board_tiebreak import rank_board_players

    by_board: dict[int, list[tuple[tuple[int, int, str], float, int]]] = {}
    for key, s in real_pstats.items():
        if s["tpr"] is None:
            continue  # no games played at all -- not a candidate, eligible or otherwise
        by_board.setdefault(s["boardNo"], []).append((key, s["tpr"], s["games"]))

    out: dict[int, list[dict]] = {}
    for board_no, entries in by_board.items():
        ranked = rank_board_players(entries)
        rows = []
        for r in ranked:
            s = real_pstats[r.key]
            team_info = team_names.get(s["teamNo"], {})
            rows.append({
                "teamNo": s["teamNo"],
                "fed": team_info.get("fed"),
                "team": team_info.get("name"),
                "fideId": s["fideId"],
                "name": s["name"],
                "games": s["games"],
                "tpr": s["tpr"],
                "eligible": r.eligible,
                "rank": r.rank,
            })
        out[board_no] = rows
    return out
