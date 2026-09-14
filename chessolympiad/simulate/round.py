"""Simulate (or ingest a real) round, updating all the running state a
tournament simulation needs: match points, pairing history (for the pairing
engine's no-rematch rule), tiebreak history, and per-player stats (for
board-medal TPR estimation).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chessolympiad.model.lineup import choose_lineup
from chessolympiad.pairing.swiss_team import Pairing, TeamPairingState
from chessolympiad.simulate.match import match_points_from_game_points, simulate_match
from chessolympiad.simulate.tiebreak import RoundRecord


@dataclass
class PlayerStat:
    team_no: int
    board_no: int  # the board this player's games were played at, not their nominal roster slot
    fide_id: int | None
    name: str
    score: float = 0.0
    games: int = 0
    opp_rating_sum: float = 0.0


@dataclass
class TournamentState:
    pairing_states: dict[int, TeamPairingState]
    rosters: dict[int, list[dict]]
    team_ratings: dict[int, float] = field(default_factory=dict)
    match_points: dict[int, int] = field(default_factory=dict)
    history: dict[int, list[RoundRecord]] = field(default_factory=dict)
    # keyed by (team_no, board_no, player identity) -- NOT just (team_no,
    # board_no) -- because a reserve substituting in at a board must accrue
    # their own performance-rating record there, separate from whoever
    # normally plays that board (see model.lineup.choose_lineup).
    player_stats: dict[tuple[int, int, str], PlayerStat] = field(default_factory=dict)

    def __post_init__(self):
        for team_no in self.pairing_states:
            self.match_points.setdefault(team_no, 0)
            self.history.setdefault(team_no, [])


def _player_key(fide_id: int | None, name: str) -> str:
    return str(fide_id) if fide_id else f"name:{name}"


def _record_player_result(
    state: TournamentState, team_no: int, board_no: int, fide_id: int | None, name: str, own_score: float, opp_rating: float
):
    key = (team_no, board_no, _player_key(fide_id, name))
    stat = state.player_stats.get(key)
    if stat is None:
        stat = PlayerStat(team_no=team_no, board_no=board_no, fide_id=fide_id, name=name)
        state.player_stats[key] = stat
    stat.score += own_score
    stat.games += 1
    stat.opp_rating_sum += opp_rating


def apply_pairings(state: TournamentState, pairings: list[Pairing], rng, remaining_rounds_after: int) -> None:
    """Simulate every pairing in this round and update all running state."""
    for pairing in pairings:
        a = pairing.team_a
        cmp_a_before = state.match_points[a]
        if pairing.team_b is None:
            state.match_points[a] += 2
            state.pairing_states[a].match_points = state.match_points[a]
            state.history[a].append(
                RoundRecord(opponent_no=None, game_points=4.0, cmp_before=cmp_a_before, remaining_rounds_after=remaining_rounds_after, kind="bye")
            )
            continue

        b = pairing.team_b
        cmp_b_before = state.match_points[b]
        rating_a = state.team_ratings.get(a, 0.0)
        rating_b = state.team_ratings.get(b, 0.0)
        lineup_a = choose_lineup(state.rosters.get(a, []), rating_a, rating_b, rng)
        lineup_b = choose_lineup(state.rosters.get(b, []), rating_b, rating_a, rng)
        if len(lineup_a) < 4 or len(lineup_b) < 4:
            # incomplete roster (rare data gap) -- treat as a double forfeit-free
            # skip rather than crash; team_no keeps its current match points.
            continue

        a_pts, b_pts, boards = simulate_match(lineup_a, lineup_b, pairing.team_a_white_odd_boards, rng)
        a_mp, b_mp = match_points_from_game_points(a_pts, b_pts)
        state.match_points[a] += a_mp
        state.match_points[b] += b_mp
        # keep the pairing engine's own view of each team's score in sync --
        # otherwise every round would degenerate into round-1-style pairing
        # (see TeamPairingState.match_points), since it's a separate field
        # from the tournament's running state.match_points by design (the
        # pairing engine only needs to know current standings, not full
        # history).
        state.pairing_states[a].match_points = state.match_points[a]
        state.pairing_states[b].match_points = state.match_points[b]
        state.history[a].append(RoundRecord(opponent_no=b, game_points=a_pts, cmp_before=cmp_a_before, remaining_rounds_after=remaining_rounds_after))
        state.history[b].append(RoundRecord(opponent_no=a, game_points=b_pts, cmp_before=cmp_b_before, remaining_rounds_after=remaining_rounds_after))

        for board in boards:
            white_team = a if board["white_is_team_a"] else b
            black_team = b if white_team == a else a
            white_score, black_score = {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}[board["result"]]
            _record_player_result(state, white_team, board["board_no"], board["white_fide_id"], board["white_name"], white_score, board["black_rating"])
            _record_player_result(state, black_team, board["board_no"], board["black_fide_id"], board["black_name"], black_score, board["white_rating"])


def apply_real_round(
    state: TournamentState,
    matches: list[dict],
    games: list[dict],
    remaining_rounds_after: int,
) -> None:
    """Ingest one round's *actual* results (already played in reality) into
    the same running state, so the pairing engine's opponent/color/bye
    history and the tiebreak history stay consistent for simulating the
    remaining rounds.
    """
    for m in matches:
        a, b = m["team_a_no"], m["team_b_no"]
        cmp_a_before = state.match_points[a]
        if m.get("is_bye"):
            state.match_points[a] += 2
            state.pairing_states[a].match_points = state.match_points[a]
            state.history[a].append(
                RoundRecord(opponent_no=None, game_points=4.0, cmp_before=cmp_a_before, remaining_rounds_after=remaining_rounds_after, kind="bye")
            )
            state.pairing_states[a].had_bye = True
            continue
        cmp_b_before = state.match_points[b]
        state.match_points[a] += m["team_a_match_pts"]
        state.match_points[b] += m["team_b_match_pts"]
        state.pairing_states[a].match_points = state.match_points[a]
        state.pairing_states[b].match_points = state.match_points[b]
        state.history[a].append(RoundRecord(opponent_no=b, game_points=m["team_a_game_pts"], cmp_before=cmp_a_before, remaining_rounds_after=remaining_rounds_after))
        state.history[b].append(RoundRecord(opponent_no=a, game_points=m["team_b_game_pts"], cmp_before=cmp_b_before, remaining_rounds_after=remaining_rounds_after))
        state.pairing_states[a].opponents.add(b)
        state.pairing_states[b].opponents.add(a)

    for g in games:
        a, b = g["team_a_no"], g["team_b_no"]
        if g["result"] not in ("1-0", "0-1", "1/2-1/2"):
            continue
        white_score, black_score = {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}[g["result"]]
        white_team = g["white_team_no"]
        black_team = b if white_team == a else a
        white_board = _board_no_for(state, white_team, g["white_fide_id"], g["white_name"])
        black_board = _board_no_for(state, black_team, g["black_fide_id"], g["black_name"])
        if white_board:
            _record_player_result(state, white_team, white_board, g["white_fide_id"], g["white_name"], white_score, g["black_rating"] or 0)
        if black_board:
            _record_player_result(state, black_team, black_board, g["black_fide_id"], g["black_name"], black_score, g["white_rating"] or 0)


def _board_no_for(state: TournamentState, team_no: int, fide_id: int | None, name: str) -> int | None:
    roster = state.rosters.get(team_no, [])
    for p in roster:
        if fide_id and p.get("fide_id") == fide_id:
            return p["board_no"]
    for p in roster:
        if p.get("name") == name:
            return p["board_no"]
    return None
