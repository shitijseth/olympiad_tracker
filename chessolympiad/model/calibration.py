"""Fit the Davidson model's (nu_c0, nu_c1, white_adv) on real historical
game-level data, and rewrite model/constants.py with the result.

This is meant to be run once (or re-run when more historical data is added),
not at simulation runtime -- see the project README's calibration section.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

RESULT_TO_SCORE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}
CONSTANTS_PATH = Path(__file__).resolve().parent / "constants.py"


def load_calibration_games(conn) -> pd.DataFrame:
    """Real, decisive-or-drawn, non-forfeit games with both ratings known --
    the only rows informative about the outcome model."""
    df = pd.read_sql_query(
        """
        SELECT white_rating, black_rating, result
        FROM games
        WHERE forfeit = 0
          AND result IN ('1-0','0-1','1/2-1/2')
          AND white_rating IS NOT NULL AND white_rating > 0
          AND black_rating IS NOT NULL AND black_rating > 0
        """,
        conn,
    )
    return df


def _neg_log_likelihood(params: np.ndarray, rw: np.ndarray, rb: np.ndarray, white_score: np.ndarray) -> float:
    c0, c1, white_adv = params
    rw_adj = rw + white_adv
    avg = (rw + rb) / 2.0
    nu = np.exp(c0 + c1 * avg / 400.0)
    a = 10 ** (rw_adj / 400.0)
    b = 10 ** (rb / 400.0)
    d = nu * 10 ** ((rw_adj + rb) / 800.0)
    denom = a + b + d
    p_white = a / denom
    p_draw = d / denom
    p_black = b / denom

    ll = np.where(
        white_score == 1.0,
        np.log(np.clip(p_white, 1e-12, None)),
        np.where(
            white_score == 0.0,
            np.log(np.clip(p_black, 1e-12, None)),
            np.log(np.clip(p_draw, 1e-12, None)),
        ),
    )
    return -ll.sum()


def fit(df: pd.DataFrame) -> dict:
    rw = df["white_rating"].to_numpy(dtype=float)
    rb = df["black_rating"].to_numpy(dtype=float)
    white_score = df["result"].map(RESULT_TO_SCORE).to_numpy(dtype=float)

    x0 = np.array([-2.6, 1.0, 32.0])
    res = minimize(
        _neg_log_likelihood,
        x0,
        args=(rw, rb, white_score),
        method="Nelder-Mead",
        options={"xatol": 1e-4, "fatol": 1e-4, "maxiter": 2000},
    )
    c0, c1, white_adv = res.x
    n = len(df)
    avg_nll_null = _neg_log_likelihood(x0, rw, rb, white_score) / n
    avg_nll_fit = res.fun / n
    return {
        "nu_c0": c0,
        "nu_c1": c1,
        "white_adv": white_adv,
        "n_games": n,
        "avg_neg_log_lik": avg_nll_fit,
        "avg_neg_log_lik_at_x0": avg_nll_null,
        "converged": res.success,
    }


def write_constants(fit_result: dict) -> None:
    text = CONSTANTS_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    out = []
    for line in lines:
        if line.startswith("NU_C0:"):
            out.append(f"NU_C0: float = {fit_result['nu_c0']:.6f}\n")
        elif line.startswith("NU_C1:"):
            out.append(f"NU_C1: float = {fit_result['nu_c1']:.6f}\n")
        elif line.startswith("WHITE_ADV:"):
            out.append(f"WHITE_ADV: float = {fit_result['white_adv']:.6f}\n")
        else:
            out.append(line)
    CONSTANTS_PATH.write_text("".join(out), encoding="utf-8")


if __name__ == "__main__":
    from chessolympiad.data import db

    conn = db.connect()
    try:
        games = load_calibration_games(conn)
        print(f"Calibration set: {len(games)} games")
        if len(games) < 100:
            raise SystemExit("Not enough game data to calibrate -- run data sync first.")
        result = fit(games)
        print(result)
        write_constants(result)
        print(f"Wrote fitted constants to {CONSTANTS_PATH}")
    finally:
        conn.close()
