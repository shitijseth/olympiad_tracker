"""Empirical study of candidate factors for improving head-to-head realism,
run against real 2022+2024 game data before building anything. Every test
compares actual outcomes to the *already-calibrated* Davidson model's
predictions, so what we're looking for is a systematic, non-noise residual
the current model is missing -- not just restating the calibration fit.

Run: python -m chessolympiad.analysis.factor_study
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from chessolympiad.data import db
from chessolympiad.model import elo

FIDE_LISTS_DIR = Path(__file__).resolve().parents[2] / "data" / "fide_lists"
# Reference month ~6 months before each event, used to test whether
# pre-event rating *momentum* predicts at-event performance beyond the
# at-event rating alone. Source: fideratings.com's bulk historical list API
# (https://fideratings.com/api.html) -- top-5000-by-rating snapshots, so
# this test only has coverage for players rated above that month's cutoff.
REFERENCE_MONTH = {
    "2022-open": "2022-02", "2022-women": "2022-02",
    "2024-open": "2024-03", "2024-women": "2024-03",
}

HOST_FEDERATION = {
    "2022-open": "IND", "2022-women": "IND",
    "2024-open": "HUN", "2024-women": "HUN",
}


def load_games_with_expectation(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT g.tournament_id, g.round, g.team_a_no, g.team_b_no, g.board_no,
               g.white_team_no, g.white_fide_id, g.white_name, g.white_rating,
               g.black_fide_id, g.black_name, g.black_rating, g.result
        FROM games g
        WHERE g.forfeit = 0 AND g.result IN ('1-0','0-1','1/2-1/2')
          AND g.white_rating IS NOT NULL AND g.white_rating > 0
          AND g.black_rating IS NOT NULL AND g.black_rating > 0
        """,
        conn,
    )
    p_white, p_draw, p_black = zip(*[elo.outcome_probs(r.white_rating, r.black_rating) for r in df.itertuples()])
    df["expected_white"] = np.array(p_white) + 0.5 * np.array(p_draw)
    df["actual_white"] = df["result"].map({"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5})
    df["residual_white"] = df["actual_white"] - df["expected_white"]
    df["black_team_no"] = np.where(df.white_team_no == df.team_a_no, df.team_b_no, df.team_a_no)
    return df


def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[,\.]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return " ".join(sorted(name.split()))


def _player_identity_frame(games: pd.DataFrame) -> pd.DataFrame:
    """Long-format one-row-per-player-per-game, identified by (team_no, name)
    since historical games don't carry a FIDE ID (rosters weren't fetched
    for 2022/2024 -- only game-level rating/result data was)."""
    white = games[["tournament_id", "round", "white_team_no", "white_name", "residual_white"]].rename(
        columns={"white_team_no": "team_no", "white_name": "name", "residual_white": "residual"}
    )
    black = games[["tournament_id", "round", "black_team_no", "black_name", "residual_white"]].rename(
        columns={"black_team_no": "team_no", "black_name": "name"}
    )
    black["residual"] = -games["residual_white"].to_numpy()
    return pd.concat([white, black], ignore_index=True)


def _mean_se(x: pd.Series) -> tuple[float, float, int]:
    x = x.dropna()
    return x.mean(), x.std(ddof=1) / math.sqrt(len(x)), len(x)


def test_home_advantage(conn, games: pd.DataFrame) -> None:
    print("\n=== 1. Home advantage ===")
    teams = pd.read_sql_query("SELECT tournament_id, team_no, federation FROM teams", conn)
    fed = teams.set_index(["tournament_id", "team_no"])["federation"]

    for tid, host in HOST_FEDERATION.items():
        g = games[games.tournament_id == tid].copy()
        g["white_fed"] = list(fed.reindex(list(zip(g.tournament_id, g.white_team_no))))
        black_team = np.where(g.white_team_no == g.team_a_no, g.team_b_no, g.team_a_no)
        g["black_fed"] = list(fed.reindex(list(zip(g.tournament_id, black_team))))

        host_residuals = pd.concat([
            g.loc[g.white_fed == host, "residual_white"],
            -g.loc[g.black_fed == host, "residual_white"],
        ])
        other_residuals = pd.concat([
            g.loc[g.white_fed != host, "residual_white"],
            -g.loc[g.black_fed != host, "residual_white"],
        ])
        hm, hse, hn = _mean_se(host_residuals)
        om, ose, on = _mean_se(other_residuals)
        print(f"{tid}: host={host} n={hn} mean_residual={hm:+.4f} (SE {hse:.4f})  "
              f"vs field mean={om:+.4f} (SE {ose:.4f})  z={((hm-om)/math.sqrt(hse**2+ose**2)):+.2f}")

    print("Reading: z>~2 would mean the host effect is unlikely to be noise. |z|<1 means no real signal.")


def test_team_day_correlation(conn, games: pd.DataFrame) -> None:
    print("\n=== 2. Team-day correlation (do boards within a match move together?) ===")
    games = games.copy()
    games["team_a_residual"] = np.where(
        games.white_team_no == games.team_a_no, games.residual_white, -games.residual_white
    )
    wide = games.pivot_table(
        index=["tournament_id", "round", "team_a_no", "team_b_no"],
        columns="board_no", values="team_a_residual", aggfunc="first",
    )
    wide.columns = [f"board{c}" for c in wide.columns]
    corr = wide.corr()
    print(corr.round(3))
    pairs = [("board1", "board2"), ("board1", "board3"), ("board1", "board4"),
             ("board2", "board3"), ("board2", "board4"), ("board3", "board4")]
    vals = [corr.loc[a, b] for a, b in pairs if a in corr and b in corr]
    print(f"Average pairwise board-residual correlation within a match: {np.mean(vals):+.3f} "
          f"(n_matches~{wide.dropna().shape[0]})")
    print("Reading: 0 means boards are independent (current model's assumption). "
          "A meaningfully positive number means real matches swing together more than independent boards would.")


def test_form_persistence(conn, games: pd.DataFrame) -> None:
    print("\n=== 3. Does first-half in-event form predict second-half results? ===")
    for tid in games.tournament_id.unique():
        g = games[games.tournament_id == tid]
        max_round = g["round"].max()
        mid = max_round // 2
        long = _player_identity_frame(g)
        long = long.dropna(subset=["name"])
        key = ["team_no", "name"]
        first = long[long["round"] <= mid].groupby(key).agg(r1=("residual", "mean"), n1=("residual", "size"))
        second = long[long["round"] > mid].groupby(key).agg(r2=("residual", "mean"), n2=("residual", "size"))
        merged = first.join(second, how="inner")
        merged = merged[(merged.n1 >= 2) & (merged.n2 >= 2)]
        if len(merged) < 10:
            print(f"{tid}: not enough players with games in both halves (n={len(merged)}), skipping")
            continue
        corr = merged["r1"].corr(merged["r2"])
        se = 1 / math.sqrt(len(merged) - 3) if len(merged) > 3 else float("nan")  # Fisher z approx SE
        print(f"{tid}: n_players={len(merged)}  corr(first-half residual, second-half residual)={corr:+.3f}  (approx SE {se:.3f})")
    print("Reading: correlation near 0 means a hot/cold start has no predictive value beyond rating "
          "(the adaptive-rating feature would be adding noise, not signal). A clearly positive, "
          "consistent correlation across events would justify it.")


def test_federation_bias(conn, games: pd.DataFrame) -> None:
    print("\n=== 4. Federation-level systematic bias (min 20 games per event) ===")
    teams = pd.read_sql_query("SELECT tournament_id, team_no, federation FROM teams", conn)
    fed = teams.set_index(["tournament_id", "team_no"])["federation"]

    per_event = {}
    for tid in games.tournament_id.unique():
        g = games[games.tournament_id == tid].copy()
        g["white_fed"] = list(fed.reindex(list(zip(g.tournament_id, g.white_team_no))))
        black_team = np.where(g.white_team_no == g.team_a_no, g.team_b_no, g.team_a_no)
        g["black_fed"] = list(fed.reindex(list(zip(g.tournament_id, black_team))))
        long = pd.concat([
            g[["white_fed", "residual_white"]].rename(columns={"white_fed": "federation", "residual_white": "residual"}),
            g[["black_fed", "residual_white"]].rename(columns={"black_fed": "federation"}).assign(residual=lambda d: -g["residual_white"]),
        ])
        agg = long.groupby("federation")["residual"].agg(["mean", "count"])
        per_event[tid] = agg[agg["count"] >= 20]["mean"]

    section_pairs = [("2022-open", "2024-open"), ("2022-women", "2024-women")]
    for a, b in section_pairs:
        both = per_event[a].to_frame("y2022").join(per_event[b].to_frame("y2024"), how="inner")
        both["consistent_sign"] = np.sign(both.y2022) == np.sign(both.y2024)
        both = both.sort_values("y2024", ascending=False)
        print(f"\n{a} vs {b} (federations with >=20 qualifying games in both):")
        print(both.round(3).to_string())
        print(f"Fraction with same-sign residual both years: {both.consistent_sign.mean():.2f} "
              f"(0.5 = pure noise, higher = persistent federation effect)")


def test_recent_form_momentum(conn, games: pd.DataFrame) -> None:
    print("\n=== 5. Does pre-event rating MOMENTUM predict at-event performance "
          "beyond the at-event rating itself? ===")
    teams = pd.read_sql_query("SELECT tournament_id, team_no, federation FROM teams", conn)
    fed = teams.set_index(["tournament_id", "team_no"])["federation"]

    for tid, ref_month in REFERENCE_MONTH.items():
        path = FIDE_LISTS_DIR / f"{ref_month}.json"
        if not path.exists():
            print(f"{tid}: reference list {ref_month} not found at {path}, skipping")
            continue
        ref = json.loads(path.read_text())
        ref_df = pd.DataFrame(ref["players"])
        ref_df["key"] = ref_df["name"].map(_normalize_name) + "|" + ref_df["country"]
        dupe_keys = ref_df["key"][ref_df["key"].duplicated(keep=False)].unique()
        ref_df = ref_df[~ref_df["key"].isin(dupe_keys)]
        ref_lookup = ref_df.set_index("key")["rating"].astype(float)
        min_rating_covered = ref_df["rating"].astype(float).min()

        g = games[games.tournament_id == tid].copy()
        g["white_fed"] = list(fed.reindex(list(zip(g.tournament_id, g.white_team_no))))
        g["black_fed"] = list(fed.reindex(list(zip(g.tournament_id, g.black_team_no))))
        white = g[["white_name", "white_fed", "white_rating", "residual_white"]].rename(
            columns={"white_name": "name", "white_fed": "fed", "white_rating": "rating", "residual_white": "residual"}
        )
        black = g[["black_name", "black_fed", "black_rating", "residual_white"]].rename(
            columns={"black_name": "name", "black_fed": "fed", "black_rating": "rating"}
        )
        black["residual"] = -g["residual_white"].to_numpy()
        long = pd.concat([white, black], ignore_index=True).dropna(subset=["name", "fed"])
        per_player = long.groupby(["name", "fed"]).agg(
            at_event_rating=("rating", "first"), mean_residual=("residual", "mean"), n_games=("residual", "size")
        ).reset_index()
        per_player = per_player[per_player.n_games >= 3]

        per_player["key"] = per_player["name"].map(_normalize_name) + "|" + per_player["fed"]
        per_player["rating_ref_month"] = per_player["key"].map(ref_lookup)
        matched = per_player.dropna(subset=["rating_ref_month"]).copy()
        matched["momentum"] = matched["at_event_rating"] - matched["rating_ref_month"]

        corr = matched["momentum"].corr(matched["mean_residual"])
        se = 1 / math.sqrt(len(matched) - 3) if len(matched) > 3 else float("nan")
        print(f"{tid}: matched {len(matched)}/{len(per_player)} eligible players against the {ref_month} list "
              f"(that list only covers ratings >= {min_rating_covered:.0f}); "
              f"corr(momentum since {ref_month}, at-event residual)={corr:+.3f} (approx SE {se:.3f}), "
              f"mean |momentum|={matched['momentum'].abs().mean():.0f} pts")
    print("Reading: correlation near 0 means the at-event rating already fully absorbs recent form -- "
          "a player who gained/lost points in the prior ~6 months performs at-event exactly as their "
          "*current* rating predicts, no better or worse. A clearly positive correlation would mean "
          "the current rating still lags true strength and momentum should be added as its own factor.")


if __name__ == "__main__":
    conn = db.connect()
    try:
        games = load_games_with_expectation(conn)
        print(f"Loaded {len(games)} real, decisive-or-drawn, non-forfeit games across "
              f"{games.tournament_id.nunique()} tournaments.")
        test_home_advantage(conn, games)
        test_team_day_correlation(conn, games)
        test_form_persistence(conn, games)
        test_federation_bias(conn, games)
        test_recent_form_momentum(conn, games)
    finally:
        conn.close()
