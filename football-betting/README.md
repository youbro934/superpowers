# Autonomous Football Betting System (paper trading)

Runs every hour on GitHub Actions, on GitHub's servers, so your computer doesn't need to be on. Each run it:

1. **Recovers** any bet a crashed run left half-placed. It never places one twice.
2. **Settles** open bets using 90-minute results (football-data.org first, The Odds API scores as fallback;
   corners and cards come from API-Football or football-data.co.uk).
3. **Scans** 17 leagues worldwide for matches in the next 36 h, using the free Odds API event listing.
   Favorite teams (Barcelona, Real Madrid, Bayern, PSG, Benfica, Sporting CP, FC Porto, Flamengo) get first claim on the odds budget,
   plus extra markets (BTTS, double chance).
4. **Models** each league with a time-weighted Dixon-Coles Poisson model, fitted daily on the last two seasons from
   football-data.co.uk. Corners and cards use the same rating method (plain Poisson, held to a higher bar).
5. **Prices** each market: match winner, double chance, over/under, BTTS, and corners/cards when odds exist. It blends the model 50/50 with
   the bookmakers' de-vigged consensus, then computes expected value at the best available price.
6. **Decides.** It picks the highest-EV single, or a 2–3-leg combo from different matches, with total odds between 1.30 and 1.40.
   Every leg must clear its own edge and probability bars, so weak legs are never added just to reach the odds. If nothing qualifies, the answer is
   **NO BET**.
7. **Executes** through the paper executor. No money moves.
8. **Pushes** notifications to your iPhone and rebuilds the dashboard and CSV exports.

## Bankroll rules (enforced in `fbs/bankroll.py`, covered by tests)

| Rule | Behaviour |
|---|---|
| Initial bankroll | €10, the only deposit ever. It is hard-capped at €10 even if the setting is raised. |
| Stake | 100% of the current bankroll, i.e. the full gross return of the last win (€10 → €14 → €19.60). |
| Bets per day | At most 2, and only one open at a time, so the second bet waits for the first to settle. |
| Daily limit | Never filled for its own sake; NO BET is a normal outcome. |
| €500 | Betting is blocked and an urgent push goes out with bankroll and net profit. It resumes only on the exact command `CONTINUE`. |
| Loss | The bankroll goes to €0. With no deposits allowed, the system stops for good. |

## The risk, in numbers

From €10 to €500 at odds 1.30–1.40 takes **12–15 wins in a row**. One loss ends the run.

* If the odds were exactly fair, the chance of reaching €500 before going broke is 10/500 = **2%**.
* At a typical 1.40 price (about a 70% chance per bet), 12 straight wins happen **about 1.4%** of the time.
* Even with a real +3% edge on every bet, the chance stays around **2–3%**.

So the expected outcome of this strategy is losing the €10, about 97 times in 100. €300/month net profit cannot come
from a €10 all-in bankroll that is never topped up. Whether the model has any edge at all has to be measured. Run the backtest and keep paper trading for weeks before considering real money.

## One-time setup (needs your accounts; I can't create these for you)

1. **Free API keys**
   * The Odds API: https://the-odds-api.com (free plan, 500 credits/month). The system spreads the credits evenly
     across the month.
   * football-data.org: https://www.football-data.org/client/register (free plan, used for results).
   * Optional: API-Football (https://www.api-football.com, free 100 requests/day) for injuries and corner/card stats.
2. **iPhone notifications.** Install the free **ntfy** app and subscribe to a long random topic name, e.g.
   `fbs-` followed by 20 random characters. Anyone who knows the topic can read it, so keep it secret.
   Telegram also works (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`).
3. **GitHub secrets** (repo → Settings → Secrets and variables → Actions):
   `ODDS_API_KEY`, `FOOTBALL_DATA_ORG_KEY`, `NTFY_TOPIC`, plus optionally `API_FOOTBALL_KEY`, `HEALTHCHECK_URL`
   (a free https://healthchecks.io check, which alerts you if the hourly runs stop arriving at all).
4. **Turn the scheduler on.** Merge this branch into `main` (GitHub only runs scheduled workflows from the default branch).
   Then open the **Actions** tab and enable workflows; forks have them disabled by default.
5. **Test.** Actions → *Football Betting System* → *Run workflow* → `notify-test`. A push should arrive on your phone.
6. **Optional dashboard.** Settings → Pages → deploy from branch `fbs-data`, folder `/`, then set the repository
   variable `FBS_DASHBOARD_URL` to the Pages URL so tapping a notification opens it. This repository is public,
   so the dashboard and the `fbs-data` branch are public too.

## Commands

From the Actions tab (*Run workflow*), or locally with `python -m fbs --data-dir DIR <command>`:

| Command | What it does |
|---|---|
| `run` | One full cycle (the hourly job). |
| `status` | Bankroll, state, open bets. |
| `continue` | Resume after the €500 stop. The confirm box must say exactly `CONTINUE`. |
| `backtest` | Walk-forward test over the last 365 days of closing odds. Writes `backtest.json` and pushes the summary to your phone. |
| `notify-test` | Sends a test push. |
| `settle LEG_ID void` | Local only: void a postponed or abandoned selection. |

## Notifications you get

Paper bets placed, wins and losses with the new bankroll, a daily NO BET notice (after 22:00 Lisbon time), the €500 stop,
a lost bankroll, data-provider failures (after 2 in a row), model-reliability warnings (when, over 30+ settled selections, the model scores worse than the bookmakers' odds),
settlements that need attention, failed workflow runs, and (with healthchecks.io) runs that stop happening.

## Real-money execution: not active

`FBS_EXECUTION_MODE` is `paper`. Setting it to `live` is refused. No authorized bookmaker adapter exists yet, and
live mode also requires the variable `FBS_LIVE_CONFIRMATION="I AUTHORIZE REAL MONEY BETTING"`. An adapter must use an
officially documented betting API (the Betfair Exchange API is the best-known one; whether you can use it depends on
your country's licensing) and implement `fbs/execution.Executor`. That includes `find(client_ref)`, so a retried run
can never place the same bet twice. Nothing in this project scrapes bookmaker websites or works around their controls.

## Layout

```
fbs/config.py          rules, thresholds, leagues, secrets from env
fbs/models.py          Poisson ratings + Dixon-Coles, market probabilities
fbs/decision.py        de-vig, EV, singles/combos, NO BET
fbs/bankroll.py        compound bankroll, ledger, €500 stop, CONTINUE
fbs/engine.py          the hourly cycle, budgets, recovery, alerts
fbs/settlement.py      90-minute settlement rules
fbs/providers/         The Odds API, football-data.org, API-Football, football-data.co.uk
fbs/execution/         paper executor + interface for an authorized live adapter
fbs/notify.py          ntfy / Telegram with a persistent outbox
fbs/dashboard.py       static HTML dashboard + CSV exports
fbs/backtest.py        walk-forward backtest
tests/                 offline tests on SYNTHETIC data (python -m unittest discover -s tests -t .)
```

Uses only the Python standard library: there are no dependencies to install.

## Known limits

* Champions League and other cross-league matches aren't modelled: team ratings from different leagues aren't
  comparable, so those matches are skipped rather than guessed.
* Only half-goal lines (2.5, 1.5…) are traded, so there are no pushes or Asian quarter lines.
* Injury data only raises the required edge. It doesn't change the probabilities.
* Corners and cards are only bet if your Odds API plan returns those markets (`FBS_SECONDARY_MARKETS`).
* Paper bets take the best price among the bookmakers returned. Set `FBS_ALLOWED_BOOKMAKERS` to the books you could
  actually use (e.g. `betclic,pinnacle`), or paper results will look better than reality.
