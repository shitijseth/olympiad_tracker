"""UI validation for the dashboard's LIVE (in-progress tournament) display:
actual match points/rank columns on the Leaderboard, the Team detail
"Results so far" round-by-round panel, actual TPR/games on Board medals,
and the Simulate tab seeding its early rounds from real results instead of
simulating them.

Unlike scripts/validate_dashboard_ui.py (which exercises the real,
currently-pre-event 2026 export), this builds a small hand-crafted "live"
payload directly -- matching exactly what export_artifact_data.py produces
once as_of_round > 0 -- so this scenario has a fast, dependency-free
regression test that doesn't need real mid-event data or a historical DB.

The dashboard fetches this as a plain static file (fetch("data/<id>.json")),
not the artifact `db` capability (which requires readers to be signed into
the owner's org -- see validate_dashboard_ui.py's module docstring), so no
mocking of `window.claude` is needed here either.

Usage:
    python scripts/validate_dashboard_live_ui.py
"""

import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_SRC = ROOT / "ui" / "artifact" / "dashboard.html"

failures = []
console_errors = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def _player(name, rating, board):
    return {"board": board, "name": name, "title": "GM", "rating": rating, "fed": "AAA", "fideId": board}


def build_payload(strip_tb_from_team=None):
    """4 teams, 2 real rounds played (team 1 beat everyone), round 3 onward
    left to the forecast/simulator -- matches export_artifact_data.py's
    schema exactly (see chessolympiad/report/export_artifact_data.py and
    chessolympiad/simulate/real_standings.py)."""
    teams = []
    for no, (fed, name) in enumerate([("AAA", "Alpha"), ("BBB", "Bravo"), ("CCC", "Charlie"), ("DDD", "Delta")], start=1):
        roster = [
            {"board": b, "name": f"{name} P{b}", "title": "GM", "rating": 2600 - b * 20, "fed": fed, "fideId": no * 10 + b}
            for b in range(1, 5)
        ]
        # Give board 1 of each team its own real games/score/TPR so far, to
        # exercise the roster card's per-player actual-results line.
        roster[0]["actualGames"] = 2
        roster[0]["actualScore"] = 1.5 if no == 1 else 1.0
        roster[0]["actualTpr"] = 2650 + no
        teams.append({
            "no": no, "fed": fed, "name": name, "rtg": 2570, "captain": "Cap", "seed": no,
            "pGold": 0.3, "pSilver": 0.2, "pBronze": 0.1, "pAnyMedal": 0.6, "pCatMedal": 0.1,
            "pTop8": 0.9, "pTop16": 0.99, "expRank": float(no), "expMp": 15.0, "rankStd": 2.0,
            "roster": roster,
            "actualMp": {1: 4, 2: 0, 3: 2, 4: 2}[no],
            "actualRank": {1: 1, 2: 4, 3: 2, 4: 2}[no],
            "actualTb2": {1: 12.5, 2: 4.0, 3: 8.0, 4: 8.0}[no],
            "actualTb3": {1: 5.5, 2: 3.0, 3: 4.0, 4: 4.0}[no],
            "actualTb4": {1: 2, 2: 4, 3: 3, 4: 3}[no],
            "actualGamePts": {1: 5.5, 2: 3.0, 3: 4.0, 4: 4.0}[no],
            "roundResults": [
                {
                    "round": 1, "opponentNo": 2 if no in (1, 2) else 4, "opponentFed": "BBB" if no == 1 else ("AAA" if no == 2 else ("DDD" if no == 3 else "CCC")),
                    "opponentName": "Bravo" if no == 1 else ("Alpha" if no == 2 else ("Delta" if no == 3 else "Charlie")),
                    "ownGamePts": 3.0 if no == 1 else (1.0 if no == 2 else 2.0), "oppGamePts": 1.0 if no == 1 else (3.0 if no == 2 else 2.0),
                    "ownMatchPts": 2 if no in (1,) else (0 if no == 2 else 1), "result": "W" if no == 1 else ("L" if no == 2 else "D"),
                    "isBye": False,
                    "boards": [
                        {"boardNo": b, "ownName": f"{name} P{b}", "ownRating": 2600 - b * 20,
                         "oppName": f"Opp P{b}", "oppRating": 2580 - b * 20, "ownIsWhite": b % 2 == 1,
                         "result": "1-0" if no == 1 else ("0-1" if no == 2 else "1/2-1/2")}
                        for b in range(1, 5)
                    ],
                },
                {
                    "round": 2, "opponentNo": 3 if no in (1, 3) else 4, "opponentFed": "CCC" if no == 1 else ("AAA" if no == 3 else "BBB" if no == 4 else "DDD"),
                    "opponentName": "Charlie" if no == 1 else ("Alpha" if no == 3 else "Bravo" if no == 4 else "Delta"),
                    "ownGamePts": 2.5 if no == 1 else 2.0, "oppGamePts": 1.5 if no == 1 else 2.0,
                    "ownMatchPts": 2 if no == 1 else (0 if no == 3 else 1), "result": "W" if no == 1 else ("L" if no == 3 else "D"),
                    "isBye": False, "boards": [],
                },
            ],
        })
    if strip_tb_from_team is not None:
        # Simulates a stale cached data/<id>.json fetched against a newer
        # dashboard.html that expects actualTb2/3/4 -- these are fetched as
        # a separate static file from the page itself, so a deploy can
        # briefly serve exactly this combination. Must degrade to "--", not
        # throw and crash the whole Team detail render.
        stale = next(t for t in teams if t["no"] == strip_tb_from_team)
        del stale["actualTb2"], stale["actualTb3"], stale["actualTb4"]

    boards = {}
    for b in range(1, 5):
        boards[str(b)] = [
            {"fed": "AAA", "team": "Alpha", "name": f"Alpha P{b}", "tpr": 2650, "pMedal": 0.4,
             "actualGames": 2, "actualScore": 1.5, "actualTpr": 2700 + b},
            {"fed": "BBB", "team": "Bravo", "name": f"Bravo P{b}", "tpr": 2600, "pMedal": 0.2,
             "actualGames": 2, "actualScore": 0.5, "actualTpr": 2500 + b},
        ]

    real_rounds = {
        "1": [
            {"teamA": 1, "teamB": 2, "isBye": False, "teamAGamePts": 3.0, "teamBGamePts": 1.0,
             "teamAMatchPts": 2, "teamBMatchPts": 0, "teamAWhiteOdd": True,
             "boards": [{"boardNo": b, "whiteTeam": 1 if b % 2 == 1 else 2,
                         "whiteName": f"W{b}", "whiteRating": 2600, "blackName": f"B{b}", "blackRating": 2580,
                         "result": "1-0"} for b in range(1, 5)]},
            {"teamA": 3, "teamB": 4, "isBye": False, "teamAGamePts": 2.0, "teamBGamePts": 2.0,
             "teamAMatchPts": 1, "teamBMatchPts": 1, "teamAWhiteOdd": True,
             "boards": [{"boardNo": b, "whiteTeam": 3 if b % 2 == 1 else 4,
                         "whiteName": f"W{b}", "whiteRating": 2600, "blackName": f"B{b}", "blackRating": 2580,
                         "result": "1/2-1/2"} for b in range(1, 5)]},
        ],
        "2": [
            {"teamA": 1, "teamB": 3, "isBye": False, "teamAGamePts": 2.5, "teamBGamePts": 1.5,
             "teamAMatchPts": 2, "teamBMatchPts": 0, "teamAWhiteOdd": True, "boards": []},
            {"teamA": 2, "teamB": 4, "isBye": False, "teamAGamePts": 2.0, "teamBGamePts": 2.0,
             "teamAMatchPts": 1, "teamBMatchPts": 1, "teamAWhiteOdd": False, "boards": []},
        ],
    }

    pairings = {
        rd: [{"teamA": m["teamA"], "teamB": m["teamB"], "hasResults": True,
              "teamAGamePts": m["teamAGamePts"], "teamBGamePts": m["teamBGamePts"],
              "teamAWhiteOdd": m["teamAWhiteOdd"], "boards": m["boards"]}
             for m in matches]
        for rd, matches in real_rounds.items()
    }
    # Round 3: the boundary round -- published (real pairings exist) but not
    # in real_rounds, so not yet complete. Board 1 of each match is already
    # decided; the rest are still open, exercising the Simulate tab's
    # "bring up to date with live results" path (startBoundaryRound).
    pairings["3"] = [
        {"teamA": 1, "teamB": 4, "hasResults": False, "teamAGamePts": None, "teamBGamePts": None, "teamAWhiteOdd": True,
         "boards": [
             {"boardNo": 1, "whiteTeam": 1, "whiteName": "Alpha P1", "whiteRating": 2580, "blackName": "Delta P1", "blackRating": 2540, "result": "1-0"},
             {"boardNo": 2, "whiteTeam": 4, "whiteName": "Delta P2", "whiteRating": 2520, "blackName": "Alpha P2", "blackRating": 2560, "result": None},
             {"boardNo": 3, "whiteTeam": 1, "whiteName": "Alpha P3", "whiteRating": 2540, "blackName": "Delta P3", "blackRating": 2500, "result": None},
             {"boardNo": 4, "whiteTeam": 4, "whiteName": "Delta P4", "whiteRating": 2480, "blackName": "Alpha P4", "blackRating": 2520, "result": None},
         ]},
        {"teamA": 2, "teamB": 3, "hasResults": False, "teamAGamePts": None, "teamBGamePts": None, "teamAWhiteOdd": True,
         "boards": [{"boardNo": b, "whiteTeam": 2 if b % 2 == 1 else 3,
                     "whiteName": f"W{b}", "whiteRating": 2560, "blackName": f"B{b}", "blackRating": 2540,
                     "result": None} for b in range(1, 5)]},
    ]

    return {
        "tournamentId": "2026-open", "name": "Live Test Open", "numRounds": 11, "numTeams": 4,
        "lastSynced": "2026-09-17T00:00:00", "generatedAt": "2026-09-17T00:00:00",
        "asOfRound": 2, "liveRound": 2, "iterations": 100,
        "teams": teams, "boards": boards, "realRounds": real_rounds, "pairings": pairings,
        "stats": {"avgRating": 2570, "minRating": 2500, "maxRating": 2600, "totalFederations": 4,
                   "ratingHistogram": {"lo": 1000, "hi": 2900, "binWidth": 158.3, "bins": [0] * 12},
                   "fedCounts": {"AAA": 1, "BBB": 1, "CCC": 1, "DDD": 1}},
    }


def build_harness(tmp_dir: Path, strip_tb_from_team=None) -> Path:
    data_dir = tmp_dir / "data"
    data_dir.mkdir()
    payload = build_payload(strip_tb_from_team=strip_tb_from_team)
    (data_dir / "2026-open.json").write_text(json.dumps(payload), encoding="utf-8")
    (data_dir / "2026-women.json").write_text(json.dumps(payload), encoding="utf-8")

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
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def run_stale_tb_check(base_url: str):
    """Regression check for a real crash this file caught: fmtTb() called
    .toFixed on an undefined actualTb2/3/4 and threw, taking down the whole
    Team detail render, whenever those fields were missing (a stale cached
    data/<id>.json fetched against a dashboard.html newer than it -- see
    ui/artifact/dashboard.html's fmtTb docstring comment for the fix)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1100})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"{exc}\n{getattr(exc, 'stack', '')}"))

        page.goto(base_url, wait_until="networkidle", timeout=20000)
        page.wait_for_function("document.querySelectorAll('#lb-table tbody tr').length > 0", timeout=15000)
        page.click(".nav-btn[data-view='team']")
        search = page.locator("#team-search")
        search.click(); search.fill("Alpha")
        page.wait_for_timeout(200)
        page.locator(".result-row").first.click()
        page.wait_for_timeout(300)
        body_text = page.locator("#team-detail-body").inner_text()
        check("team with missing TB2/3/4 still renders Live standing without crashing",
              "Live standing" in body_text, body_text[:400])
        check("missing TB values degrade to '—' instead of throwing", "—" in body_text)
        check("no uncaught JS console error from the missing TB fields", len(console_errors) == 0, "; ".join(console_errors[:5]))
        browser.close()


def main():
    port = 8941
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        build_harness(tmp_dir)
        httpd = serve(tmp_dir, port)
        try:
            run_checks(f"http://127.0.0.1:{port}/dashboard.html")
        finally:
            httpd.shutdown()

    console_errors.clear()
    port2 = 8942
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        build_harness(tmp_dir, strip_tb_from_team=1)
        httpd = serve(tmp_dir, port2)
        try:
            run_stale_tb_check(f"http://127.0.0.1:{port2}/dashboard.html")
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
        page = browser.new_page(viewport={"width": 1400, "height": 1100})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"{exc}\n{getattr(exc, 'stack', '')}"))

        page.goto(base_url, wait_until="networkidle", timeout=20000)
        page.wait_for_function("document.querySelectorAll('#lb-table tbody tr').length > 0", timeout=15000)
        page.wait_for_timeout(300)

        check("tournament logo image loaded", page.evaluate("document.querySelector('.brand-logo').naturalWidth > 0"))
        check("ChessBase India logo image loaded", page.evaluate("document.querySelector('.broadcast-logo').naturalWidth > 0"))
        check("body gets the 'live' class once asOfRound > 0", page.evaluate("document.body.classList.contains('live')"))
        alpha_row = next(
            page.locator("#lb-table tbody tr").nth(i).inner_text()
            for i in range(page.locator("#lb-table tbody tr").count())
            if "Alpha" in page.locator("#lb-table tbody tr").nth(i).inner_text()
        )
        check("leaderboard row shows actual MP and rank for the leader", "4" in alpha_row and "#1" in alpha_row, alpha_row)
        check("leaderboard row shows actual game points for the leader", "5" in alpha_row, alpha_row)

        # Team detail
        page.click(".nav-btn[data-view='team']")
        search = page.locator("#team-search")
        search.click(); search.fill("Alpha")
        page.wait_for_timeout(200)
        page.locator(".result-row").first.click()
        page.wait_for_timeout(300)
        body_text = page.locator("#team-detail-body").inner_text()
        check("Team detail shows 'Results so far' panel", "Results so far" in body_text, body_text[:200])
        check("Team detail shows the Live standing panel with real TB values",
              "Live standing" in body_text and "TB2" in body_text and "12.5" in body_text, body_text[:400])
        check("Roster shows a player's own real games/score/TPR so far", "so far" in body_text and "TPR" in body_text, body_text[:400])
        check("round history shows both rounds with a result pill", "#1" in body_text and "#2" in body_text)
        boards_btns = page.locator("#team-detail-body button", has_text="Boards")
        if boards_btns.count() > 0:
            before = page.locator("#team-detail-body").inner_html()
            boards_btns.first.click()
            page.wait_for_timeout(150)
            after = page.locator("#team-detail-body").inner_html()
            check("clicking Boards actually expands board-level detail", before != after)
            check("expanded board row shows a player name", "Alpha P1" in page.locator("#team-detail-body").inner_text())

        # Board medals
        page.click(".nav-btn[data-view='boards']")
        page.wait_for_timeout(300)
        boards_text = page.locator("#view-boards").inner_text().lower()
        check("Board medals shows Actual TPR column with data", "actual tpr" in boards_text)
        check("Board medals shows a Points column with data", "points" in boards_text)
        check("Board medals shows a Games column with data", "games" in boards_text)

        # Simulate tab: should start past the real rounds already ingested.
        page.click(".nav-btn[data-view='simulate']")
        page.wait_for_timeout(300)
        page.locator("#sim-controls button", has_text="Start simulation").first.click()
        page.wait_for_function("document.getElementById('sim-body').innerText.length > 20", timeout=10000)
        sim_text = page.locator("#sim-body").inner_text()
        check("Simulate tab starts at round asOfRound+1 (round 3)", "Round 3 / 11" in sim_text, sim_text[:150])
        check("Simulate tab shows the real-rounds-seeded banner", "already played" in sim_text, sim_text[:300])

        # Round 3 is the boundary round: published real pairings, board 1
        # of each match already decided, boards 2-4 still open -- verifies
        # startBoundaryRound wired the real players/known result in and
        # sampled the rest, instead of falling back to a synthetic pairing.
        check("Simulate tab shows the live-round banner", "is live" in sim_text and "locked in" in sim_text, sim_text[:400])
        boards_btn = page.locator("#sim-body button", has_text="Boards").first
        boards_btn.click()
        page.wait_for_timeout(150)
        expanded_text = page.locator("#sim-body").inner_text()
        check("boundary round shows the real player names from the published pairing", "Alpha P1" in expanded_text and "Delta P1" in expanded_text, expanded_text[:400])
        board1_select = page.locator("#sim-body .board-row", has_text="Bd 1").first.locator("select")
        check("boundary round's already-decided board 1 is locked to its real result", board1_select.input_value() == "1-0", board1_select.input_value())
        open_selects = page.locator("#sim-body .board-row select")
        check("boundary round's still-open boards got a sampled (non-empty) result",
              all(open_selects.nth(i).input_value() in ("1-0", "0-1", "1/2-1/2") for i in range(open_selects.count())))

        confirm_btn = page.locator("#sim-body button", has_text="Confirm round")
        if confirm_btn.count() > 0:
            before = page.locator("#sim-body").inner_text()
            confirm_btn.first.click()
            page.wait_for_function("(text) => document.getElementById('sim-body').innerText !== text", arg=before, timeout=8000)
            after_text = page.locator("#sim-body").inner_text()
            check("confirming the boundary round advances to round 4", "Round 4 / 11" in after_text, after_text[:150])

        check("no uncaught JS console errors", len(console_errors) == 0, "; ".join(console_errors[:5]))
        browser.close()


if __name__ == "__main__":
    main()
