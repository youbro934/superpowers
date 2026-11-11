BEGIN TRANSACTION;
CREATE TABLE bets (
  id INTEGER PRIMARY KEY, client_ref TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
  local_date TEXT NOT NULL, slot INTEGER NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
  stake_cents INTEGER NOT NULL, odds REAL NOT NULL, prob REAL NOT NULL, ev REAL NOT NULL,
  external_ref TEXT, settled_at TEXT, return_cents INTEGER, note TEXT);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, local_date TEXT NOT NULL, outcome TEXT NOT NULL,
  reason TEXT, detail TEXT);
CREATE TABLE ledger (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, balance_cents INTEGER NOT NULL, bet_id INTEGER, note TEXT);
INSERT INTO "ledger" VALUES(1,'2026-11-11T17:36:38Z','DEPOSIT',1000,1000,NULL,'initial bankroll (the only deposit ever allowed)');
CREATE TABLE legs (
  id INTEGER PRIMARY KEY, bet_id INTEGER NOT NULL, event_id TEXT NOT NULL, sport_key TEXT,
  competition TEXT, home TEXT, away TEXT, kickoff TEXT, market TEXT, selection TEXT, line REAL,
  odds REAL, bookmaker TEXT, p_model REAL, p_market REAL, p_used REAL, ev REAL,
  result TEXT NOT NULL DEFAULT 'pending', home_goals INTEGER, away_goals INTEGER,
  stat_total INTEGER, settled_at TEXT, source TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO "meta" VALUES('status','"ACTIVE"');
INSERT INTO "meta" VALUES('initial_cents','1000');
INSERT INTO "meta" VALUES('backtest','{"period": "2025-11-11..2026-11-11", "bets": 0, "brier_1x2": {"model": 0.6034667355001078, "market": 0.587630255091467, "matches": 5831}, "value_legs": {"n": 94, "hit_rate": 0.5, "expected_hit_rate": 0.6381638297872341, "avg_odds": 1.6263829787234043, "avg_claimed_ev": 0.03507021276595745, "flat_stake_roi": -0.19744680851063828}, "brier_by_model_weight": {"0.0": 0.587630255091467, "0.25": 0.5895962457965082, "0.5": 0.592890989432961, "0.75": 0.5975144860008271, "1.0": 0.6034667355001078}, "flat_stake_roi": null}');
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, dedupe_key TEXT, title TEXT, body TEXT,
  priority TEXT, tags TEXT, sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
INSERT INTO "notifications" VALUES(1,'2026-11-11T17:40:43Z',NULL,'Backtest finished','Backtest 2025-11-11..2026-11-11: 0 qualifying bets. Brier model/market: {''model'': 0.6034667355001078, ''market'': 0.587630255091467, ''matches'': 5831}','default','bar_chart',0,0,NULL);
INSERT INTO "notifications" VALUES(2,'2026-11-11T17:50:16Z',NULL,'Backtest finished','Backtest 2025-11-11..2026-11-11: 0 bets met every rule. 1X2 Brier model 0.6035 vs market 0.5876 over 5831 matches (model LESS accurate than bookmakers). 94 selections flagged as value: won 50.0% vs 63.8% expected, avg odds 1.63, flat-stake ROI -19.7%.','default','bar_chart',0,0,NULL);
INSERT INTO "notifications" VALUES(3,'2026-11-11T17:50:16Z',NULL,'Model reliability warning','On historical data the model is less accurate than the bookmakers'' closing odds, so its ''value'' signals are not evidence of a real edge. Expect NO BET most days.','high','warning',0,0,NULL);
CREATE TABLE provider_status (
  provider TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT, last_error_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ratings (competition TEXT PRIMARY KEY, fitted_at TEXT, model TEXT);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, summary TEXT);
COMMIT;
