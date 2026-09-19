"""Command-line entry point.

    python -m chessolympiad.cli init-db
    python -m chessolympiad.cli sync-2026            # idempotent full resync of both 2026 sections
    python -m chessolympiad.cli sync-historical       # one-time: 2022 + 2024 game data for calibration
    python -m chessolympiad.cli calibrate             # refit the Davidson model on historical data
    python -m chessolympiad.cli backtest              # validate the calibrated model against 2022/2024
    python -m chessolympiad.cli simulate open --iterations 3000
    python -m chessolympiad.cli simulate women --iterations 3000
    python -m chessolympiad.cli live-update open       # re-sync + re-simulate remaining rounds during the event
"""

from __future__ import annotations

import typer

from chessolympiad.data import db
from chessolympiad.data.sync import sync_tournament

app = typer.Typer(add_completion=False)

TOURNAMENTS = {
    "open": ("2026-open", 1469895),
    "women": ("2026-women", 1469896),
}
HISTORICAL = [
    ("2022-open", 653631, "open", 2022),
    ("2022-women", 653632, "women", 2022),
    ("2024-open", 967173, "open", 2024),
    ("2024-women", 967172, "women", 2024),
]


@app.command()
def init_db():
    """Create the SQLite schema (safe to re-run)."""
    db.init_db()
    typer.echo(f"DB ready at {db.DB_PATH}")


@app.command()
def sync_2026(section: str = typer.Argument(None, help="'open', 'women', or omit for both")):
    """Full idempotent resync of the live 2026 event(s) from chess-results.com."""
    sections = [section] if section else list(TOURNAMENTS.keys())
    for s in sections:
        tid, tnr = TOURNAMENTS[s]
        result = sync_tournament(tid, tnr, s, 2026, progress=typer.echo, fetch_rosters=True)
        typer.echo(f"{tid}: {result}")


@app.command()
def sync_historical():
    """One-time ingest of 2022 + 2024 game data (used only for calibration/backtesting)."""
    for tid, tnr, section, year in HISTORICAL:
        result = sync_tournament(tid, tnr, section, year, progress=typer.echo, fetch_rosters=False)
        typer.echo(f"{tid}: {result}")


@app.command()
def calibrate():
    """Refit the Davidson outcome model on historical game data and update model/constants.py."""
    from chessolympiad.model import calibration

    conn = db.connect()
    try:
        games = calibration.load_calibration_games(conn)
        typer.echo(f"Calibration set: {len(games)} games")
        result = calibration.fit(games)
        typer.echo(str(result))
        calibration.write_constants(result)
        typer.echo("Wrote model/constants.py")
    finally:
        conn.close()


@app.command()
def backtest():
    """Validate the calibrated model against the known 2022/2024 outcomes."""
    from chessolympiad.backtest.run_backtest import run_backtest as _run

    for tid, *_ in HISTORICAL:
        res = _run(tid, iterations=500)
        typer.echo(str(res))


@app.command()
def simulate(section: str, iterations: int = 3000):
    """Run the Monte Carlo forecast for one 2026 section and write reports/*.{csv,md}."""
    from chessolympiad.report.report import simulate_and_store, write_reports

    tid, _tnr = TOURNAMENTS[section]
    run_id = simulate_and_store(tid, iterations=iterations, progress=typer.echo)
    csv_path, md_path = write_reports(tid, run_id)
    typer.echo(f"run_id={run_id}\nwrote {csv_path}\nwrote {md_path}")


@app.command()
def live_update(section: str, iterations: int = 3000, full_refresh: bool = False):
    """Re-sync real results (idempotent) then re-simulate the remaining rounds.

    Board-level round results now come primarily from lichess.org's
    broadcast of the event (chessolympiad.data.lichess_sync) -- faster than
    chess-results.com (no manual arbiter entry step) and, unlike
    chess-results.com, has no daily request cap to worry about. chess-results.com
    is still the source for the team list (every call, cheap) and rosters
    (see full_refresh) since Lichess broadcasts don't carry seed/initial-rank
    or full reserve rosters.

    full_refresh defaults to False, which skips work that's almost entirely
    wasted on a frequent/recurring call: re-fetching every team's roster
    (rosters rarely change once the event starts) and re-verifying
    chess-results.com's own board results for rounds Lichess already has
    (kept only as an occasional cross-check/backstop against Lichess, not
    the primary path). Pass --full-refresh explicitly (or let the daily
    cron cadence do it) to re-verify everything against both live sites.
    """
    tid, tnr = TOURNAMENTS[section]
    result = sync_tournament(
        tid,
        tnr,
        section,
        2026,
        progress=typer.echo,
        fetch_rosters=full_refresh,
        refetch_complete_rounds=full_refresh,
        fetch_round_results=full_refresh,
    )
    typer.echo(f"synced (chess-results.com): {result}")

    from chessolympiad.data import lichess_sync

    conn = db.connect()
    try:
        num_rounds = conn.execute(
            "SELECT num_rounds FROM tournaments WHERE tournament_id = ?", (tid,)
        ).fetchone()["num_rounds"]
        written = lichess_sync.sync_tournament_from_lichess(conn, tid, section, num_rounds, progress=typer.echo)
        typer.echo(f"synced (lichess): {written} boards written")
    finally:
        conn.close()

    from chessolympiad.report.report import simulate_and_store, write_reports

    run_id = simulate_and_store(tid, iterations=iterations, progress=typer.echo)
    csv_path, md_path = write_reports(tid, run_id)
    typer.echo(f"run_id={run_id}\nwrote {csv_path}\nwrote {md_path}")


if __name__ == "__main__":
    app()
