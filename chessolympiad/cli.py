"""Command-line entry point.

    python -m chessolympiad.cli init-db
    python -m chessolympiad.cli sync-2026            # idempotent full resync of both 2026 sections
    python -m chessolympiad.cli sync-historical       # one-time: 2022 + 2024 game data for calibration
    python -m chessolympiad.cli calibrate             # refit the Davidson model on historical data
    python -m chessolympiad.cli backtest              # validate the calibrated model against 2022/2024
    python -m chessolympiad.cli simulate open --iterations 3000
    python -m chessolympiad.cli simulate open --iterations 5000 --max-workers 4
    python -m chessolympiad.cli simulate women --iterations 3000
    python -m chessolympiad.cli sync-only open         # re-sync real results only, no resimulation
    python -m chessolympiad.cli live-update open       # sync-only + simulate combined, for manual/one-off use
    python -m chessolympiad.cli deep-simulate-if-needed open  # high-iteration pass, once per completed round

    The live 2026 event runs sync-only + simulate (5000 iters, 4 workers)
    and deep-simulate-if-needed (20000 iters, 1 worker, once per completed
    round) on two independent cron cadences (see scripts/sync_data.sh and
    scripts/auto_update.sh) rather than live-update's combined form --
    results can usefully be fetched far more often than it's worth spending
    CPU re-running Monte Carlo.
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
def simulate(section: str, iterations: int = 3000, max_workers: int = 1):
    """Run the Monte Carlo forecast for one 2026 section and write reports/*.{csv,md}.

    max_workers > 1 splits the run across worker processes (see
    chessolympiad.simulate.tournament.run_monte_carlo_parallel), which
    itself never uses more than cpu_count-1 workers regardless of what's
    requested here, so an unattended cron call can't starve the machine.
    """
    from chessolympiad.report.report import simulate_and_store, write_reports

    tid, _tnr = TOURNAMENTS[section]
    run_id = simulate_and_store(tid, iterations=iterations, progress=typer.echo, max_workers=max_workers)
    csv_path, md_path = write_reports(tid, run_id)
    typer.echo(f"run_id={run_id}\nwrote {csv_path}\nwrote {md_path}")


@app.command()
def deep_simulate_if_needed(section: str, iterations: int = 20000):
    """Run a high-iteration, single-process forecast once a round has fully
    completed -- meant to run on the same cadence as `simulate` but only
    actually resimulate the first time it's called after a round wraps up.
    Tracks progress via simulation_runs.notes='deep': compares the highest
    as_of_round any 'deep' run has covered against the current as_of_round,
    and no-ops if that round has already gotten its deep run.
    """
    from chessolympiad.report.report import simulate_and_store, write_reports
    from chessolympiad.simulate.loader import load_real_rounds

    tid, _tnr = TOURNAMENTS[section]
    conn = db.connect()
    try:
        real_rounds = load_real_rounds(conn, tid, complete_only=True)
        as_of_round = max(real_rounds.keys()) if real_rounds else 0
        row = conn.execute(
            "SELECT MAX(as_of_round) AS r FROM simulation_runs WHERE tournament_id = ? AND notes = 'deep'",
            (tid,),
        ).fetchone()
        last_deep = row["r"] if row and row["r"] is not None else 0
    finally:
        conn.close()

    if as_of_round == 0 or as_of_round <= last_deep:
        typer.echo(f"{tid}: as_of_round={as_of_round}, last deep run covered round {last_deep} -- nothing to do")
        return

    run_id = simulate_and_store(tid, iterations=iterations, progress=typer.echo, notes="deep")
    csv_path, md_path = write_reports(tid, run_id)
    typer.echo(f"{tid}: deep run_id={run_id} (as_of_round={as_of_round})\nwrote {csv_path}\nwrote {md_path}")


def _sync_section(section: str, full_refresh: bool, progress) -> None:
    """Re-sync real results for one 2026 section (idempotent, no simulation).

    Board-level round results come primarily from lichess.org's broadcast
    of the event (chessolympiad.data.lichess_sync) -- faster than
    chess-results.com (no manual arbiter entry step) and, unlike
    chess-results.com, has no daily request cap to worry about.
    chess-results.com is still the source for the team list (every call,
    cheap) and rosters (see full_refresh) since Lichess broadcasts don't
    carry seed/initial-rank or full reserve rosters.

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
        progress=progress,
        fetch_rosters=full_refresh,
        refetch_complete_rounds=full_refresh,
        fetch_round_results=full_refresh,
    )
    progress(f"synced (chess-results.com): {result}")

    from chessolympiad.data import lichess_sync

    conn = db.connect()
    try:
        num_rounds = conn.execute(
            "SELECT num_rounds FROM tournaments WHERE tournament_id = ?", (tid,)
        ).fetchone()["num_rounds"]
        written = lichess_sync.sync_tournament_from_lichess(conn, tid, section, num_rounds, progress=progress)
        progress(f"synced (lichess): {written} boards written")
    finally:
        conn.close()


@app.command()
def sync_only(section: str, full_refresh: bool = False):
    """Re-sync real results for one 2026 section -- no resimulation. Meant
    for a tight cron cadence (results can usefully be checked far more
    often than it's worth spending CPU re-running Monte Carlo); pair with
    `simulate` on its own, slower cadence.
    """
    _sync_section(section, full_refresh, typer.echo)


@app.command()
def live_update(section: str, iterations: int = 3000, full_refresh: bool = False, max_workers: int = 1):
    """sync-only + simulate combined, for manual/one-off use. The live 2026
    event runs these on two independent cron cadences instead (see
    scripts/sync_data.sh and scripts/auto_update.sh).
    """
    _sync_section(section, full_refresh, typer.echo)

    from chessolympiad.report.report import simulate_and_store, write_reports

    tid, _tnr = TOURNAMENTS[section]
    run_id = simulate_and_store(tid, iterations=iterations, progress=typer.echo, max_workers=max_workers)
    csv_path, md_path = write_reports(tid, run_id)
    typer.echo(f"run_id={run_id}\nwrote {csv_path}\nwrote {md_path}")


if __name__ == "__main__":
    app()
