"""SQLite store. The persistent copy is a plain-text SQL dump (fbs.sql), so the
state branch in git gets small readable diffs instead of binary blobs."""
from __future__ import annotations

import json
import os
import sqlite3

from .util import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, balance_cents INTEGER NOT NULL, bet_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY, client_ref TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
  local_date TEXT NOT NULL, slot INTEGER NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
  stake_cents INTEGER NOT NULL, odds REAL NOT NULL, prob REAL NOT NULL, ev REAL NOT NULL,
  external_ref TEXT, settled_at TEXT, return_cents INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS legs (
  id INTEGER PRIMARY KEY, bet_id INTEGER NOT NULL, event_id TEXT NOT NULL, sport_key TEXT,
  competition TEXT, home TEXT, away TEXT, kickoff TEXT, market TEXT, selection TEXT, line REAL,
  odds REAL, bookmaker TEXT, p_model REAL, p_market REAL, p_used REAL, ev REAL,
  result TEXT NOT NULL DEFAULT 'pending', home_goals INTEGER, away_goals INTEGER,
  stat_total INTEGER, settled_at TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, local_date TEXT NOT NULL, outcome TEXT NOT NULL,
  reason TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT, local_date TEXT, event_id TEXT NOT NULL,
  sport_key TEXT, competition TEXT, home TEXT, away TEXT, kickoff TEXT, market TEXT NOT NULL,
  selection TEXT NOT NULL, line REAL, first_odds REAL, last_odds REAL, bookmaker TEXT, p_model REAL,
  p_market REAL, p_used REAL, ev REAL, reason TEXT, result TEXT, settled_at TEXT,
  UNIQUE(event_id, market, selection, line));
CREATE TABLE IF NOT EXISTS fixture_results (
  fixture_id INTEGER PRIMARY KEY, home_goals INTEGER, away_goals INTEGER, ht_home INTEGER, ht_away INTEGER,
  stats TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS predictions (
  fixture_id INTEGER PRIMARY KEY, created_at TEXT, league TEXT, home TEXT, away TEXT, kickoff TEXT,
  model_version TEXT, params TEXT);
CREATE TABLE IF NOT EXISTS shadow_entries (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, local_date TEXT NOT NULL, odds REAL, prob REAL, ev REAL,
  legs TEXT NOT NULL, result TEXT, settled_at TEXT, UNIQUE(local_date, legs));
CREATE TABLE IF NOT EXISTS ratings (competition TEXT PRIMARY KEY, fitted_at TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, dedupe_key TEXT, title TEXT, body TEXT,
  priority TEXT, tags TEXT, sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS provider_status (
  provider TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT, last_error_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.dir = data_dir
        self.dump_path = os.path.join(data_dir, "fbs.sql")
        self.db_path = os.path.join(data_dir, "fbs.sqlite")
        if os.path.exists(self.dump_path):
            # the dump is the source of truth; rebuild the working db from it
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            self.conn = sqlite3.connect(self.db_path)
            with open(self.dump_path, encoding="utf-8") as f:
                self.conn.executescript(f.read())
        else:
            self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- helpers ---
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, args=()) -> int:
        cur = self.conn.execute(sql, args)
        return cur.lastrowid

    def commit(self):
        self.conn.commit()
        self.export()

    def get(self, key: str, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def export(self):
        tmp = self.dump_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for line in self.conn.iterdump():
                f.write(line + "\n")
        os.replace(tmp, self.dump_path)

    def prune(self):
        self.x("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 400)")
        self.x("DELETE FROM notifications WHERE sent=1 AND id NOT IN "
               "(SELECT id FROM notifications ORDER BY id DESC LIMIT 1000)")

    # --- provider health ---
    def provider_ok(self, provider: str):
        self.x("INSERT INTO provider_status(provider,last_ok,consecutive_failures) VALUES(?,?,0) "
               "ON CONFLICT(provider) DO UPDATE SET last_ok=excluded.last_ok, consecutive_failures=0",
               (provider, iso(utcnow())))

    def provider_failed(self, provider: str, error: str) -> int:
        self.x("INSERT INTO provider_status(provider,last_error,last_error_at,consecutive_failures) "
               "VALUES(?,?,?,1) ON CONFLICT(provider) DO UPDATE SET last_error=excluded.last_error, "
               "last_error_at=excluded.last_error_at, consecutive_failures=consecutive_failures+1",
               (provider, error[:500], iso(utcnow())))
        return self.one("SELECT consecutive_failures FROM provider_status WHERE provider=?", (provider,))[0]

    def close(self):
        self.conn.close()
