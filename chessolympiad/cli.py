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
    python -m chessolympiad.cli tick                   # one live-scheduler step (see scripts/scheduler.sh)

    The live 2026 event runs `tick` on a single 1-minute cron cadence (see
    scripts/scheduler.sh); tick itself decides, from the fixed round
    calendar in chessolympiad/schedule.py plus each section's actual
    completion state, whether to run the fast in-round cadence (lichess
    every tick, resimulate + check for a completed round's deep run every
    5 min) or the slow between-rounds cadence (chess-results.com only,
    every 2h). The other commands above remain for manual/one-off use.
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


def _deep_simulate_if_needed(section: str, iterations: int = 20000, progress=None) -> None:
    """Run a high-iteration, single-process forecast once a round has fully
    completed -- meant to be called on a tight cadence but only actually
    resimulate the first time it's called after a round wraps up. Tracks
    progress via simulation_runs.notes='deep': compares the highest
    as_of_round any 'deep' run has covered against the current as_of_round,
    and no-ops if that round has already gotten its deep run.
    """
    from chessolympiad.report.report import simulate_and_store, write_reports
    from chessolympiad.simulate.loader import load_real_rounds

    progress = progress or (lambda _msg: None)
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
        progress(f"{tid}: as_of_round={as_of_round}, last deep run covered round {last_deep} -- nothing to do")
        return

    run_id = simulate_and_store(tid, iterations=iterations, progress=progress, notes="deep")
    csv_path, md_path = write_reports(tid, run_id)
    progress(f"{tid}: deep run_id={run_id} (as_of_round={as_of_round})\nwrote {csv_path}\nwrote {md_path}")


@app.command()
def deep_simulate_if_needed(section: str, iterations: int = 20000):
    """CLI wrapper around _deep_simulate_if_needed for manual/one-off use."""
    _deep_simulate_if_needed(section, iterations, progress=typer.echo)


def _sync_chessresults_only(section: str, full_refresh: bool, progress) -> None:
    """Re-sync one 2026 section from chess-results.com only: team list
    (every call, cheap), whatever round is in progress, and -- always,
    regardless of official start time -- a probe for the next round's
    pairings/board order, since chess-results.com routinely publishes
    those hours before the round officially starts (see sync.py's module
    docstring). full_refresh additionally re-fetches rosters and
    re-verifies already-decided rounds; both are wasted on a frequent call
    so default off, exercised only periodically.
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


def _sync_lichess_only(section: str, progress) -> int:
    """Re-sync one 2026 section's board-level results from lichess.org's
    broadcast (faster than chess-results.com, no manual arbiter-entry step,
    no daily request cap). Returns the number of new boards written.
    """
    from chessolympiad.data import lichess_sync

    tid, _tnr = TOURNAMENTS[section]
    conn = db.connect()
    try:
        num_rounds = conn.execute(
            "SELECT num_rounds FROM tournaments WHERE tournament_id = ?", (tid,)
        ).fetchone()["num_rounds"]
        written = lichess_sync.sync_tournament_from_lichess(conn, tid, section, num_rounds, progress=progress)
        conn.commit()
        progress(f"synced (lichess): {written} boards written")
        return written
    finally:
        conn.close()


def _sync_section(section: str, full_refresh: bool, progress) -> None:
    """Re-sync real results for one 2026 section (idempotent, no
    simulation) -- both sources, for manual/one-off use (`sync-only`,
    `live-update`). The live event's own cron cadence (chessolympiad.cli.tick)
    calls _sync_chessresults_only and _sync_lichess_only independently
    instead, on their own separate schedules.
    """
    _sync_chessresults_only(section, full_refresh, progress)
    _sync_lichess_only(section, progress)


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
    event runs `tick` on its own cron cadence instead (see scripts/scheduler.sh).
    """
    _sync_section(section, full_refresh, typer.echo)

    from chessolympiad.report.report import simulate_and_store, write_reports

    tid, _tnr = TOURNAMENTS[section]
    run_id = simulate_and_store(tid, iterations=iterations, progress=typer.echo, max_workers=max_workers)
    csv_path, md_path = write_reports(tid, run_id)
    typer.echo(f"run_id={run_id}\nwrote {csv_path}\nwrote {md_path}")


def _both_sections_complete(round_no: int) -> bool:
    from chessolympiad.simulate.loader import load_real_rounds

    conn = db.connect()
    try:
        for section in TOURNAMENTS:
            tid, _tnr = TOURNAMENTS[section]
            real_rounds = load_real_rounds(conn, tid, complete_only=True)
            if round_no not in real_rounds:
                return False
        return True
    finally:
        conn.close()


ACTIVE_SIM_INTERVAL = 300  # 5 min
NOT_ACTIVE_FETCH_INTERVAL = 7200  # 2 hours
FULL_REFRESH_INTERVAL = 79200  # ~22h, drifts earlier each day rather than later
IDLE_TIMEOUT = 3600  # fall back to not-active if a round goes this long with no new lichess results


@app.command()
def tick():
    """Single entry point for the live cron scheduler -- see
    scripts/scheduler.sh, meant to be invoked every minute. Decides,
    from the fixed round calendar (chessolympiad.schedule) plus each
    section's actual completion state plus an idle timeout, which of two
    phases we're in right now:

      - ACTIVE (inside a round's window, that round not yet complete for
        both sections, and results have arrived within the last
        IDLE_TIMEOUT seconds): sync lichess every tick, and every
        ACTIVE_SIM_INTERVAL seconds resimulate (5000 iters, 4 workers) and
        check each section for a newly-completed round's deep run (20000
        iters, 1 worker -- see _deep_simulate_if_needed).
      - NOT ACTIVE (before a round starts, on the rest day, or after a
        round finishes/times out): sync chess-results.com only (team list,
        current round, and a standing probe for the next round's
        pairings/board order) every NOT_ACTIVE_FETCH_INTERVAL seconds, with
        an occasional roster full-refresh folded in.

    Prints a final "PUBLISH=1"/"PUBLISH=0" line so scripts/scheduler.sh
    knows whether anything was actually resynced/resimulated this tick and
    it's worth exporting + publishing.
    """
    import datetime as dt

    from chessolympiad import scheduler_state as state
    from chessolympiad.report.report import simulate_and_store, write_reports
    from chessolympiad.schedule import scheduled_round_for

    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.timestamp()
    round_no = scheduled_round_for(now_dt)
    did_work = False

    is_active = False
    if round_no is not None and not _both_sections_complete(round_no):
        last_round, last_epoch = state.read_progress("active_progress")
        if last_round != round_no:
            last_epoch = now
            state.write_progress("active_progress", round_no, last_epoch)
        if (now - last_epoch) < IDLE_TIMEOUT:
            is_active = True

    if is_active:
        typer.echo(f"tick: ACTIVE (round {round_no})")
        any_new = False
        for section in TOURNAMENTS:
            try:
                written = _sync_lichess_only(section, typer.echo)
            except Exception as e:  # noqa: BLE001 -- one section's transient failure (rate limit, network) must not block the other or crash the tick
                typer.echo(f"WARN: {section} lichess sync failed, continuing: {e}")
                written = 0
            if written > 0:
                any_new = True
                did_work = True
        if any_new:
            state.write_progress("active_progress", round_no, now)

        if state.due("active_sim", ACTIVE_SIM_INTERVAL, now):
            for section in TOURNAMENTS:
                tid, _tnr = TOURNAMENTS[section]
                try:
                    run_id = simulate_and_store(tid, iterations=5000, progress=typer.echo, max_workers=4)
                    write_reports(tid, run_id)
                except Exception as e:  # noqa: BLE001 -- see above
                    typer.echo(f"WARN: simulate {section} failed, continuing: {e}")
                try:
                    _deep_simulate_if_needed(section, progress=typer.echo)
                except Exception as e:  # noqa: BLE001 -- see above
                    typer.echo(f"WARN: deep-simulate-if-needed {section} failed, continuing: {e}")
            state.mark_done("active_sim", now)
            did_work = True
    else:
        typer.echo("tick: NOT ACTIVE")
        if state.due("not_active_fetch", NOT_ACTIVE_FETCH_INTERVAL, now):
            full_refresh = state.due("full_refresh", FULL_REFRESH_INTERVAL, now)
            for section in TOURNAMENTS:
                try:
                    _sync_chessresults_only(section, full_refresh, typer.echo)
                except Exception as e:  # noqa: BLE001 -- see above
                    typer.echo(f"WARN: sync-only {section} failed, continuing: {e}")
            if full_refresh:
                state.mark_done("full_refresh", now)
            state.mark_done("not_active_fetch", now)
            did_work = True

    typer.echo(f"PUBLISH={'1' if did_work else '0'}")


if __name__ == "__main__":
    app()
