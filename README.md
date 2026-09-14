# Chess Olympiad 2026 Prediction System

A Monte Carlo forecasting system for the **46th Chess Olympiad** (Samarkand,
Uzbekistan, 15–27 Sep 2026), covering both the Open (208 teams) and Women's
(190 teams) sections. Built on real, live-fetched data from
[chess-results.com](https://chess-results.com), calibrated against the two
most recent Olympiads (2022 Chennai, 2024 Budapest), and paired with an
interactive dashboard published as a Claude Artifact.

## What it does

For each section, it Monte Carlo simulates the remaining rounds of the
11-round Swiss event thousands of times and reports, per team: probability
of gold/silver/bronze/any overall medal, probability of a rating-category
medal (Article 4.10.2), probability of finishing top-8/top-16, expected
final rank, and expected match points. It also estimates per-board medal
probabilities (Article 4.10.3) from simulated tournament performance ratings.

Once the event starts, `live-update` re-syncs real round-by-board results
from chess-results.com and re-simulates only the *remaining* rounds, using
each player's blended pre-event/in-event ("adaptive") rating.

The dashboard (`ui/artifact/dashboard.html`, published as a Claude Artifact)
also includes an **interactive round-by-round simulator**: a full
client-side JS port of the pairing engine, outcome model, and lineup
rotation, so you can step through all 11 rounds, override any match or
board result, and watch standings update live -- entirely in the browser.

## Tournament rules implemented

Sourced from FIDE's official 2024 Olympiad Regulations (structurally
unchanged year to year) and the FIDE Handbook's Swiss Team Pairing System:

- **Format**: Swiss, 11 rounds, teams of 4 + 1 reserve, both sections in parallel.
- **Scoring**: match points (win=2, draw=1, loss=0) computed from each
  match's aggregate board score — not raw game points.
- **Tiebreaks (Annex 2.I)**: implemented *exactly* — the IS(10) formula
  (game points × opponent's final match points, best 10 opponents, weakest
  dropped, with the regulation's specific bye/unplayed-match substitutions),
  then total game points, then sum of the 10 best opponents' match points.
- **Medals**: overall top 3; 5 rating-category medals (teams bucketed by
  initial seed, best finisher per bucket who hasn't already won overall);
  per-board medals by simulated tournament performance rating (≥8 games).

## Data pipeline

**chess-results.com is the single data source**, for both the live 2026
event and the two historical events used for calibration — confirmed live
tournament IDs:

| Event | Open | Women |
|---|---|---|
| 2026 Samarkand | tnr1469895 | tnr1469896 |
| 2024 Budapest | tnr967173 | tnr967172 |
| 2022 Chennai | tnr653631 | tnr653632 |

Every sync (`sync-2026`, `live-update`) is an **idempotent full resync**:
it re-fetches current rosters, ratings, and every round's board-by-board
results and upserts into SQLite (`data/olympiad.db`), keyed so re-fetching
an already-synced round is a no-op and a correction on the source site is
picked up automatically. This matters because rosters/board order are only
provisional until captains submit a fixed order close to Round 1, and
captains can substitute players between rounds during the event.

Every individual board result (round, both players, ratings, color,
result) is captured — not just team-level match summaries — because that
game-level table is what both the outcome-model calibration and the
adaptive in-event rating are built on.

## Outcome model

Per-game probabilities come from the **Davidson model** (Elo-with-draws):
draw propensity is fit as a function of average player rating (grandmaster
games draw far more than club-level games at the same rating gap), plus a
fitted white-first-move-advantage constant. Fit by maximum likelihood on
**12,779 real Olympiad games** from 2022+2024 — see
`chessolympiad/model/constants.py` for the fitted values and
`chessolympiad/model/calibration.py` to re-fit.

On top of that, each match applies a **correlated "team day" shock**: a
shared random rating perturbation drawn once per match per team and applied
to all 4 of that team's boards, so results within a match move together the
way real ones do (`chessolympiad/simulate/match.py`).

## Lineup rotation

Real captains rest a board and field the reserve, especially against weak
opponents — measured directly from 2022+2024 (`chessolympiad/model/lineup.py`):
substitution rates of 17%/29%/37%/34% for boards 1-4, and rounds with a
substitution had a rating gap (own avg − opponent avg) 18-32 points more
favorable than rounds without. Modelled as a logistic P(substitution at
board b) fit on ~30,000 real per-board-per-round observations, applied at
match time (at most one substitution per match, since there's only one
reserve) — see `model/constants.py` for the fitted coefficients.

### What we checked before adding anything (factor study)

Before changing the model, four (then five) candidate factors were tested
against real game-level residuals (actual score − model-predicted score)
from 2022+2024, to avoid adding complexity that doesn't actually move the
numbers (`chessolympiad/analysis/factor_study.py`):

| Factor | Verdict | Evidence |
|---|---|---|
| Host-nation advantage | Not supported | Significant for India in 2022 Open (z=+2.96) but absent in 2022 Women and both 2024 sections — one hit in four is consistent with noise/confounding (India's 2022 rise), not a real host effect. |
| **Team-day correlation** | **Real — implemented** | Board results within a match correlate at +0.05 to +0.09 (avg +0.072) across all 6 board-pairs, over 2,811 real matches (~3.5σ from zero). Calibrated a shock SD of 87 rating points to reproduce it. |
| In-event form persistence | Real — already handled | First-half vs second-half performance correlates at +0.14 to +0.23, consistent across all 4 events (4-6σ each). Backing out the implied optimal shrinkage constant gives ≈20 games — matching the pre-existing `ADAPTIVE_RATING_FULL_WEIGHT_GAMES` value, so no change was needed. |
| Federation-level bias | Real but not modelled | 64-71% sign-consistency 2022→2024 (50% = noise) — something is there, but it's concentrated in small/inactive federations with persistently negative residuals, which looks like stale FIDE ratings (rarely-played players) rather than a "federation strength" effect. Would need FIDE activity data not currently ingested; a per-federation fudge factor on this data would just overfit two data points. |
| Pre-event rating momentum | Weak / not implemented | Tested whether a player's rating trend in the ~6 months before the Olympiad (via [fideratings.com](https://fideratings.com)'s historical monthly list API) predicts at-event performance beyond their at-event rating. Per-event correlations: +0.09, +0.03, +0.04, +0.17 — none individually distinguishable from noise, only marginal (~2.1σ) when pooled. Not implemented. |
| **Lineup rotation** | **Real — implemented** | Substitution rates 17%/29%/37%/34% by board, rounds with a substitution show an 18-32 point more favorable rating gap. Fit as a logistic model, ~30,000 observations. |

## Validated against real outcomes (backtest)

Both historical Olympiads were replayed **blind** (pre-event ratings only,
no knowledge of what actually happened) through the exact same simulator
used for the 2026 forecast, then scored against the real final standings:

| Event | Brier (medal) | Spearman rank corr. |
|---|---|---|
| 2022 Open | 0.017 | 0.95 |
| 2022 Women | 0.010 | 0.94 |
| 2024 Open | 0.010 | 0.95 |
| 2024 Women | 0.015 | 0.95 |

(Brier score for "finishes top 3": 0 is perfect. Spearman correlation
between simulated expected-rank and actual final rank: 1.0 is perfect.)
Re-run with `python -m chessolympiad.cli backtest`.

Also validated end-to-end: the JS engine that powers the dashboard's
interactive simulator was extracted and run standalone (headless, in Node)
against the real 2026 Open field for 150 auto-play trials, reproducing the
Python engine's aggregate medal probabilities closely (e.g. USA 38.7% vs
41.1% any-medal) — confirming the two independent implementations agree.

The dashboard's actual DOM/UI (not just the extracted engine) is validated
with Playwright — `scripts/validate_dashboard_ui.py` serves `dashboard.html`
locally with a mock of the artifact `db` capability (fed the real exported
forecast JSON) and drives headless Chromium through every tab and a full
11-round Simulate play-through, failing on any uncaught JS error. This
caught a real bug (`Element.append()` returning `undefined`, not the
appended node, silently breaking the Field Stats federation chart on every
render) before it reached the published artifact.

## Known simplifications

- **Pairing engine** (`chessolympiad/pairing/swiss_team.py`): a fast,
  faithful-enough approximation of FIDE's Dutch-system team pairing —
  score groups, seed-ordered top/bottom-half pairing within a group,
  no-rematch and no-repeat-bye enforcement — but *not* a full JaVaFo/
  bbpPairings-level exhaustive-backtracking implementation. At Olympiad
  scale (~200 teams) with realistic stochastic results this produces
  essentially zero rematch collisions (validated in `tests/test_pairing.py`);
  it's only stressed by adversarial, near-deterministic score sequences
  that don't occur in practice.
- **No modelling** of travel, fatigue, or a player's very recent (non-FIDE-
  rating) form, beyond the in-event adaptive rating once real rounds exist.
- **A single simulated playthrough is one random draw, not the average.**
  E.g. a ~40% any-medal team will fail to medal in roughly 6 of every 10
  auto-played tournaments in the dashboard's Simulate tab — that's the
  model working correctly, not noise. The Leaderboard tab shows the
  probability distribution across thousands of simulations; the Simulate
  tab shows one draw from it.

## Usage

```bash
pip install -r requirements.txt
python -m chessolympiad.cli init-db
python -m chessolympiad.cli sync-2026            # pulls current rosters/ratings for both sections
python -m chessolympiad.cli sync-historical      # one-time, for calibration (2022 + 2024)
python -m chessolympiad.cli calibrate            # fits model/constants.py (already done; re-run if you add more history)
python -m chessolympiad.cli backtest             # validates against known 2022/2024 outcomes
python -m chessolympiad.cli simulate open --iterations 3000
python -m chessolympiad.cli simulate women --iterations 3000
python -m chessolympiad.report.export_artifact_data   # writes data/artifact_export/*.json for the dashboard

# once the event is underway (from 16 Sep 2026):
python -m chessolympiad.cli live-update open
python -m chessolympiad.cli live-update women

# validate the dashboard's actual UI end-to-end (needs the artifact export above):
playwright install chromium   # one-time
python scripts/validate_dashboard_ui.py
```

Reports land in `reports/{tournament_id}_forecast.{md,csv}`. The dashboard
(`ui/artifact/dashboard.html`) is published as a Claude Artifact and reads
its data from the artifact's own `db` capability — after generating fresh
`data/artifact_export/*.json`, re-seed the published artifact via the
Artifact tool's `write_db` action (see the artifact's URL).

## Project layout

```
chessolympiad/
  data/            chess-results.com client + idempotent sync + SQLite schema
  model/           Davidson outcome model, calibration, lineup, adaptive rating
  pairing/         Swiss team pairing engine
  simulate/        match/round/tournament Monte Carlo simulator, exact tiebreaks
  backtest/        blind replay of 2022/2024 against real outcomes
  report/          forecast persistence, markdown/CSV report generation, artifact data export
  analysis/        empirical factor study (chessolympiad/analysis/factor_study.py)
  cli.py           also implements live-update (idempotent resync + re-simulate)
ui/artifact/dashboard.html   the published dashboard's source (Claude Artifact)
tests/             pytest: tiebreak arithmetic, pairing invariants, Elo sanity,
                   team-day shock, lineup rotation, an end-to-end stratification test
```
