"""SQLite schema and connection helper for the Olympiad prediction system.

Everything -- rosters, ratings snapshots, the game-level fact table, and
simulation outputs -- lives in one file (data/olympiad.db). Ingestion is
designed as an idempotent full-resync (see data.sync), so every table here
is written via upsert (INSERT ... ON CONFLICT ... DO UPDATE), never plain
append.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "olympiad.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id   TEXT PRIMARY KEY,   -- e.g. "2026-open", "2024-open"
    tnr             INTEGER NOT NULL,   -- chess-results.com tnr number
    section         TEXT NOT NULL CHECK (section IN ('open', 'women')),
    year            INTEGER NOT NULL,
    name            TEXT,
    num_rounds      INTEGER NOT NULL DEFAULT 11,
    fide_event_id   INTEGER,
    last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    tournament_id   TEXT NOT NULL REFERENCES tournaments(tournament_id),
    team_no         INTEGER NOT NULL,   -- chess-results snr for this team in this tournament
    federation      TEXT NOT NULL,
    team_name       TEXT NOT NULL,
    captain         TEXT,
    rating_avg      INTEGER,
    initial_rank    INTEGER,            -- seed / starting rank used for pairing
    PRIMARY KEY (tournament_id, team_no)
);

CREATE TABLE IF NOT EXISTS players (
    tournament_id   TEXT NOT NULL REFERENCES tournaments(tournament_id),
    team_no         INTEGER NOT NULL,
    board_no        INTEGER NOT NULL,   -- 1-4 = nominated board, 5 = reserve
    fide_id         INTEGER,
    name            TEXT NOT NULL,
    title           TEXT,
    federation      TEXT,
    rating           INTEGER,
    PRIMARY KEY (tournament_id, team_no, board_no)
);

-- The core fact table: one row per individual board result per match.
CREATE TABLE IF NOT EXISTS games (
    tournament_id     TEXT NOT NULL REFERENCES tournaments(tournament_id),
    round             INTEGER NOT NULL,
    team_a_no         INTEGER NOT NULL,
    team_b_no         INTEGER NOT NULL,
    board_no          INTEGER NOT NULL,   -- 1-4 (board within the match)
    white_team_no     INTEGER,            -- which of team_a/team_b had white on this board
    white_fide_id     INTEGER,
    white_name        TEXT,
    white_rating      INTEGER,
    black_fide_id     INTEGER,
    black_name        TEXT,
    black_rating      INTEGER,
    result            TEXT,               -- '1-0', '0-1', '1/2-1/2', or NULL if unplayed/unparsed
    forfeit           INTEGER NOT NULL DEFAULT 0,  -- 1 if either side's score was a forfeit token (+/-)
    PRIMARY KEY (tournament_id, round, team_a_no, team_b_no, board_no)
);

-- Team-level match result per round (derived from `games`, not fetched separately) --
-- used for match points / standings reconstruction independent of board details.
CREATE TABLE IF NOT EXISTS matches (
    tournament_id   TEXT NOT NULL REFERENCES tournaments(tournament_id),
    round           INTEGER NOT NULL,
    team_a_no       INTEGER NOT NULL,
    team_b_no       INTEGER NOT NULL,     -- NULL/0 = bye
    team_a_game_pts REAL,
    team_b_game_pts REAL,
    team_a_match_pts INTEGER,
    team_b_match_pts INTEGER,
    is_bye          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tournament_id, round, team_a_no, team_b_no)
);

-- Simulation outputs: one row per team per simulation run (aggregated over
-- all Monte Carlo iterations of that run).
CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id   TEXT NOT NULL REFERENCES tournaments(tournament_id),
    created_at      TEXT NOT NULL,
    as_of_round     INTEGER NOT NULL,     -- real rounds ingested & fixed before this sim (0 = pre-event)
    iterations      INTEGER NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS team_forecasts (
    run_id          INTEGER NOT NULL REFERENCES simulation_runs(run_id),
    team_no         INTEGER NOT NULL,
    p_gold          REAL,
    p_silver        REAL,
    p_bronze        REAL,
    p_any_medal     REAL,
    p_top8          REAL,
    p_top16         REAL,
    p_category_medal REAL,
    expected_rank   REAL,
    expected_match_pts REAL,
    rank_std        REAL,
    PRIMARY KEY (run_id, team_no)
);

CREATE TABLE IF NOT EXISTS player_forecasts (
    run_id          INTEGER NOT NULL REFERENCES simulation_runs(run_id),
    team_no         INTEGER NOT NULL,
    board_no        INTEGER NOT NULL,
    -- a reserve who substitutes in at this board occasionally competes for
    -- it alongside whoever normally plays there (model.lineup.choose_lineup),
    -- so board_no alone doesn't identify a player -- see round.PlayerStat.
    player_key      TEXT NOT NULL,
    fide_id         INTEGER,
    name            TEXT,
    p_board_medal   REAL,
    expected_tpr    REAL,
    PRIMARY KEY (run_id, team_no, board_no, player_key)
);

CREATE INDEX IF NOT EXISTS idx_games_tournament_round ON games(tournament_id, round);
CREATE INDEX IF NOT EXISTS idx_games_white_fide ON games(white_fide_id);
CREATE INDEX IF NOT EXISTS idx_games_black_fide ON games(black_fide_id);
CREATE INDEX IF NOT EXISTS idx_matches_tournament_round ON matches(tournament_id, round);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict], key_cols: list[str]) -> None:
    """Generic upsert: INSERT ... ON CONFLICT(key_cols) DO UPDATE SET the rest."""
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    update_cols = [c for c in cols if c not in key_cols]
    conflict_clause = ", ".join(key_cols)
    if update_cols:
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_clause}) DO UPDATE SET {update_clause}"
        )
    else:
        sql = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, rows)
    conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH}")
