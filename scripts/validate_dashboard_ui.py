"""End-to-end UI validation for ui/artifact/dashboard.html, driven by Playwright.

The published dashboard is a private Claude Artifact and reads its data via
the platform-injected `claude.use("db")` capability, which only exists inside
an authenticated claude.ai session -- Playwright (or any anonymous browser)
cannot load the hosted artifact URL directly. Instead, this script:

  1. Copies the real dashboard.html unmodified except for one shim: a fake
     `window.claude.use("db")` that serves the same JSON documents from
     data/artifact_export/*.json that the real artifact db would return
     (see chessolympiad/report/export_artifact_data.py), deep-frozen to
     match the real capability's documented contract ("Delivered snapshots
     and their data() are frozen").
  2. Serves that copy over a local HTTP server (fetch() needs http://, not
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

MOCK_DB_SHIM = """<script>
// The real db capability's spec (db.d.ts) is explicit: "Delivered snapshots
// and their data() are frozen." Deep-freezing here too is deliberate, not
// incidental -- it caught a real bug (rosterGrid() sorting t.roster in
// place, which throws TypeError on a frozen array in strict mode) that a
// plain mutable mock silently let through.
function deepFreeze(obj) {
  if (obj && typeof obj === "object" && !Object.isFrozen(obj)) {
    Object.values(obj).forEach(deepFreeze);
    Object.freeze(obj);
  }
  return obj;
}
window.claude = {
  use: async (cap) => {
    if (cap !== "db") return null;
    return {
      doc: (path) => ({
        onSnapshot: (cb, errCb) => {
          const id = path.split("/")[1];
          fetch("data/" + id + ".json").then(r => r.json()).then(json => {
            cb({ exists: true, data: () => deepFreeze(json) });
          }).catch(e => { if (errCb) errCb(e); });
          return () => {};
        }
      })
    };
  }
};
</script>
"""

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

    content = DASHBOARD_SRC.read_text(encoding="utf-8")
    idx = content.index("<script>")
    content = content[:idx] + MOCK_DB_SHIM + content[idx:]
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
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"{exc}\n{getattr(exc, 'stack', '')}"))

        page.goto(base_url, wait_until="networkidle", timeout=20000)
        page.wait_for_function("document.querySelectorAll('#lb-table tbody tr').length > 5", timeout=15000)
        page.wait_for_timeout(300)

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
        check("body does NOT get the 'live' class pre-event (asOfRound=0)",
              not page.evaluate("document.body.classList.contains('live')"))
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

        for view, min_len in [("boards", 20), ("stats", 20)]:
            page.click(f".nav-btn[data-view='{view}']")
            page.wait_for_timeout(400)
            text = page.locator(f"#view-{view}").inner_text()
            check(f"{view} view shows content", len(text.strip()) > min_len, text[:120])

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
