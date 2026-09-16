"""End-to-end UI validation for ui/artifact/dashboard.html, driven by Playwright.

The dashboard fetches its forecast data as a plain static file
(fetch("data/<id>.json")) published alongside the page -- deliberately not
the artifact `db` capability, which requires every reader to be signed in
as a member of the owner's org and so silently walls off anyone the link
is shared with publicly (see the "Sign in to see this artifact's data"
incident this was built to fix). That means Playwright can drive the real
file with no mocking at all. This script:

  1. Copies the real dashboard.html and data/artifact_export/*.json (as
     data/<id>.json, matching the published layout) into a scratch dir.
  2. Serves that dir over a local HTTP server (fetch() needs http://, not
     file://).
  3. Drives it with headless Chromium: every nav tab, a section switch, and
     a full 11-round play-through of the interactive Simulate tab (Start ->
     confirm each round -> Reset), asserting on rendered DOM content and
     failing on any uncaught JS console error.

Usage:
    pip install playwright && playwright install chromium
    python -m chessolympiad.cli simulate open --iterations <n>   # if forecasts are stale
    python -m chessolympiad.report.export_artifact_data
    python scripts/validate_dashboard_ui.py
"""

import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_SRC = ROOT / "ui" / "artifact" / "dashboard.html"
EXPORT_DIR = ROOT / "data" / "artifact_export"

failures = []
console_errors = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def build_harness(tmp_dir: Path) -> Path:
    if not EXPORT_DIR.exists() or not any(EXPORT_DIR.glob("*.json")):
        sys.exit(f"No exported forecast JSON found in {EXPORT_DIR} -- run "
                  "`python -m chessolympiad.report.export_artifact_data` first.")

    data_dir = tmp_dir / "data"
    data_dir.mkdir()
    for f in EXPORT_DIR.glob("*.json"):
        shutil.copy(f, data_dir / f.name)

    shutil.copytree(DASHBOARD_SRC.parent / "assets", tmp_dir / "assets")

    content = DASHBOARD_SRC.read_text(encoding="utf-8")
    wrapped = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>\n'
        + content + "\n</body></html>"
    )
    out = tmp_dir / "dashboard.html"
    out.write_text(wrapped, encoding="utf-8")
    return out


def serve(tmp_dir: Path, port: int):
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_dir), **kw)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def main():
    port = 8933
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        build_harness(tmp_dir)
        httpd = serve(tmp_dir, port)
        base_url = f"http://127.0.0.1:{port}/dashboard.html"

        try:
            run_checks(base_url)
        finally:
            httpd.shutdown()

    print("\n==== SUMMARY ====")
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("All checks passed.")


def run_checks(base_url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.set_default_timeout(60000)
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"{exc}\n{getattr(exc, 'stack', '')}"))

        page.goto(base_url, wait_until="networkidle", timeout=20000)
        page.wait_for_function("document.querySelectorAll('#lb-table tbody tr').length > 5", timeout=15000)
        page.wait_for_timeout(300)

        check("tournament logo image loaded", page.evaluate("document.querySelector('.brand-logo').naturalWidth > 0"))
        check("ChessBase India logo image loaded", page.evaluate("document.querySelector('.broadcast-logo').naturalWidth > 0"))

        def check_every_team_row_renders_roster(section_label):
            page.click(".nav-btn[data-view='leaderboard']")
            page.wait_for_timeout(200)
            row_count = page.locator("#lb-table tbody tr").count()
            broken = []
            for i in range(row_count):
                page.locator("#lb-table tbody tr").nth(i).click()
                page.wait_for_timeout(40)
                html = page.evaluate("document.getElementById('team-detail-body').innerHTML")
                if "Roster" not in html or "roster-card" not in html:
                    name = page.evaluate(
                        "document.querySelector('#team-detail-body h2') ? "
                        "document.querySelector('#team-detail-body h2').textContent : 'UNKNOWN'"
                    )
                    broken.append((i, name))
                page.click(".nav-btn[data-view='leaderboard']")
                page.wait_for_timeout(15)
            check(
                f"every team row in {section_label} renders a Roster panel ({row_count} teams)",
                len(broken) == 0,
                f"{len(broken)} broken: {broken[:10]}",
            )

        title = page.locator("#lb-title").inner_text()
        check("leaderboard title loads (Open)", "Open" in title, title)
        status_text = page.locator("#status-text").inner_text()
        check("status text shows live forecast", "Live" in status_text, status_text)
        # Self-consistency, not a hardcoded pre-event assumption: the real
        # 2026 event is under way by the time this runs, so whether the
        # 'live' class is present should just match whatever the current
        # export's liveRound field says -- 0 pre-event/between rounds,
        # >0 once any match this round is decided.
        open_export = json.loads((EXPORT_DIR / "2026-open.json").read_text())
        live_round = open_export.get("liveRound", 0)
        has_live_class = page.evaluate("document.body.classList.contains('live')")
        check("'live' class presence matches the current export's liveRound",
              has_live_class == (live_round > 0),
              f"liveRound={live_round}, has_live_class={has_live_class}")
        rows = page.locator("#lb-table tbody tr")
        check("leaderboard table has ~208 team rows", rows.count() >= 200, f"got {rows.count()}")
        first_team = rows.nth(0).inner_text()
        check("top seed appears at top of Open leaderboard", "United States" in first_team, first_team[:80])

        page.click(".switch-btn[data-section='2026-women']")
        page.wait_for_function("document.getElementById('lb-title').textContent.includes('Women')", timeout=15000)
        page.wait_for_timeout(300)
        check("switching to Women's section updates title", "Women" in page.locator("#lb-title").inner_text())
        check("Women's leaderboard has ~190 team rows", page.locator("#lb-table tbody tr").count() >= 180)

        page.click(".switch-btn[data-section='2026-open']")
        page.wait_for_function("document.getElementById('lb-title').textContent.includes('Open')", timeout=15000)
        page.wait_for_timeout(300)

        for view, min_len in [("boards", 20), ("rounds", 20), ("stats", 20)]:
            page.click(f".nav-btn[data-view='{view}']")
            page.wait_for_timeout(400)
            text = page.locator(f"#view-{view}").inner_text()
            check(f"{view} view shows content", len(text.strip()) > min_len, text[:120])

        check("Rounds view shows 11 round buttons",
              page.locator("#round-switcher button").count() == 11)

        # Team detail: click through every single team row (not just one) --
        # a weak "does the view have >20 chars of text" check would pass even
        # on the empty-state placeholder text and miss a real rendering bug.
        check_every_team_row_renders_roster("2026-open")
        page.click(".switch-btn[data-section='2026-women']")
        page.wait_for_function("document.getElementById('lb-title').textContent.includes('Women')", timeout=15000)
        page.wait_for_timeout(300)
        check_every_team_row_renders_roster("2026-women")
        page.click(".switch-btn[data-section='2026-open']")
        page.wait_for_function("document.getElementById('lb-title').textContent.includes('Open')", timeout=15000)
        page.wait_for_timeout(300)

        page.click(".nav-btn[data-view='simulate']")
        page.wait_for_timeout(400)
        start_btn = page.locator("#sim-controls button", has_text="Start simulation")
        check("Simulate tab shows a Start simulation button", start_btn.count() > 0)

        if start_btn.count() > 0:
            start_btn.first.click()
            page.wait_for_function("document.getElementById('sim-body').innerText.length > 20", timeout=10000)
            round_label = page.locator("#sim-body").inner_text()
            check("round 1 pairings render after Start simulation", "Round" in round_label)

            rounds_advanced = 0
            for _ in range(12):
                confirm_btn = page.locator("#sim-body button", has_text="Confirm round")
                if confirm_btn.count() == 0:
                    break
                before = page.locator("#sim-body").inner_text()
                confirm_btn.first.click()
                try:
                    page.wait_for_function(
                        "(text) => document.getElementById('sim-body').innerText !== text",
                        arg=before, timeout=8000,
                    )
                except PWTimeout:
                    pass
                rounds_advanced += 1

            check("reached round 11 / tournament complete", rounds_advanced >= 10, f"only advanced {rounds_advanced} rounds")
            final_text = page.locator("#sim-body").inner_text()
            check("final standings render after playing all rounds",
                  "Tournament complete" in final_text or "Standings" in final_text, final_text[:200])
            check("no leftover 'Confirm round' button once finished",
                  page.locator("#sim-body button", has_text="Confirm round").count() == 0)

            reset_btn = page.locator("#sim-controls button", has_text="Reset")
            if reset_btn.count() > 0:
                reset_btn.first.click()
                page.wait_for_timeout(300)
                check("Reset returns Simulate tab to start state",
                      page.locator("#sim-controls button", has_text="Start simulation").count() > 0)

        check("no uncaught JS console errors", len(console_errors) == 0, "; ".join(console_errors[:5]))
        browser.close()


if __name__ == "__main__":
    main()
