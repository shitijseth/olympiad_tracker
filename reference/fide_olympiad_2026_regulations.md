# FIDE Chess Olympiad 2026 — Regulations for the Main Competition

Source: https://handbook.fide.com/files/handbook/Olympiad2026MainCompetition.pdf
Fetched: 2026-09-23

Stored locally so board-medal (and team-tiebreak) logic in this codebase can
cite and be checked against the actual regulation text, not just memory or
a chess-results.com screenshot. Only the sections relevant to standings/
medals are reproduced below; the full PDF covers organisational, financial,
and logistical matters not relevant to this project.

## 4.5. Conditions of victory

**4.5.1.** In each section, matches are scored by match points. A win scores
2 points. A draw scores 1 point. A loss scores 0 points. A team with the
highest number of match points in the relevant final standings shall be
declared Olympiad Champion. The tie-break system is described in Appendix
2.I.

**4.5.2.** The standings in the combined classification (open & women) shall
be determined by the sum of places (open team + women team): the federation
having the lower number shall be declared Combined Classification Winner.
The tie-break system is described in Appendix 2.II.

## 4.6. Prizes

### 4.6.2. Team Medals

**4.6.2.1.** In each section, every member of the winning team (players,
reserves and captain) shall receive a gold medal. Similarly, the team
finishing second shall receive silver medals and the team in third place
shall receive bronze medals.

**4.6.2.2.** Before Round 3, in each section, the teams are divided by TAP
into five rating categories, on the basis of their position in the initial
overall ranking list; as far as possible, the categories shall contain equal
numbers of teams.

**4.6.2.3.** Every member of the team that finished with the highest score
for its category (excluding the highest rated), provided that it has not
won medals in accordance with Article 4.6.2.1, shall receive a gold medal.
Similarly, the team finishing with the second score shall receive silver
medals and the team in third place shall receive bronze medals.

### 4.6.3. Individual Board Medals

**4.6.3.1.** Players assigned to the same board number in their respective
team lists compete for individual board prizes namely: gold, silver and
bronze medals. **A player needs to play at least eight games to be eligible
for a board prize.**

**4.6.3.2.** The board medals shall be awarded according to players'
performance ratings (TPR). The tie-break system is described in Appendix
2.III.

## Appendix 2 — Tie-Break Procedures

### I. Team standings in Open and Women's sections (see Article 4.5.1)

The position of teams that finish with the same number of match points
shall be determined by application of the following tie-breaking procedure
in order of priority:

- **TB1** — sum of IS(10) for the 10 best team opponents (excluding either
  the round where the team had a pairing-allocated bye, or if the team did
  not have a pairing-allocated bye, the team opponent which scored the
  lowest number of matchpoints; if there is a tie for the lowest number of
  matchpoints, then the lowest ISi is excluded). Each ISi = GPi × FMPi,
  where GPi is game points scored against opponent i, FMPi is opponent i's
  final match points.
- **TB2** — number of game points scored.
- **TB3** — sum of the match points of the 10 team opponents, excluding the
  opponent with the lowest number of match points.

Any ties unbroken after TB3 remain tied, assigned the top ranking of that
set of teams.

(This is what `chessolympiad/simulate/tiebreak.py` implements — verified
byte-for-byte against the live round-5 chess-results.com Open standings.)

### II. Team standings in the combined classification (see Article 4.5.2)

- **TB1** — sum of match points scored by the Open team and Women's team.
- **TB2** — sum of the Open team's TB1 (Appendix 2.I) and Women's team's
  TB1 (Appendix 2.I).
- **TB3** — sum of the Open team's TB2 and Women's team's TB2 (Appendix
  2.I).
- **TB4** — sum of the Open team's TB3 and Women's team's TB3 (Appendix
  2.I).

Any ties unbroken after TB4 remain tied, assigned the top ranking of that
set of teams.

(Not currently implemented in this codebase — the dashboard doesn't show a
combined Open+Women classification.)

### III. Individual standings (see Article 4.6.3.2)

If two or more players have equal TPRs, the tie shall be broken as follows:

- **TB1** — greater number of games played.
- **TB2** — drawing of lots.

TB2 (drawing of lots) is a physical/random procedure carried out at the
event itself — it cannot be meaningfully predicted or replicated by this
codebase. Ties unresolved after TB1 are treated here as genuinely tied
(same rank), not arbitrarily broken, since asserting a specific order would
misrepresent what the rule actually determines.

### IV. Unplayed Matches in Tie-Break Calculations (see Appendix 2.I and 2.II)

An Unplayed Match is a match where a team was included in a round's
pairings and all games were scored as defaults (not a pairing-allocated
bye). If a team is unpaired for a round (excluding a pairing-allocated
bye), for tie-break purposes only they score 1 match point for each round
in which they are unpaired.

Calculation of GP: unplayed win GP(uw) = 4, unplayed loss GP(ul) = 0.

Calculation of FMP: unplayed win FMP(uw) = FMP + UR; unplayed win but
opponent plays no further matches FMP(uwx) = CMP + UR; unplayed loss
FMP(ul) = FMP + UR. (FMP = opponent's final match points, CMP = team's
current match points before the unplayed match, UR = rounds the opponent
was unpaired excluding pairing-allocated byes.)

Calculation of IS: IS(uw) = GP(uw) × FMP(uw); IS(uwx) = GP(uw) × FMP(uwx);
IS(ul) = GP(ul) × FMP(ul).

(This codebase's `tiebreak.py` module has its own simplified
bye/unplayed-round substitute formulas — see that file's docstring — which
predate this reference document being fetched and may be worth
cross-checking against Appendix 2.IV above in a future pass.)
