"""Push notifications to the phone: ntfy (free iOS app) and/or Telegram.

Messages are queued in the database first and sent at the end of each run,
so a network failure only delays them; unsent messages are retried next run.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from .config import Settings
from .db import Store
from .util import ProviderError, http_request, iso, parse_iso, utcnow

log = logging.getLogger("fbs")


class Notifier:
    def __init__(self, store: Store, cfg: Settings, http=http_request):
        self.s = store
        self.cfg = cfg
        self.http = http

    @property
    def channels(self) -> list[str]:
        out = []
        if self.cfg.ntfy_topic:
            out.append("ntfy")
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            out.append("telegram")
        return out

    def queue(self, title: str, body: str, priority: str = "default", tags: str = "",
              dedupe_key: str | None = None, dedupe_hours: float | None = None) -> bool:
        """Queue a message. With dedupe_key, skip if the same key was queued recently
        (dedupe_hours) or ever (dedupe_hours=None)."""
        if dedupe_key:
            r = self.s.one("SELECT ts FROM notifications WHERE dedupe_key=? ORDER BY id DESC LIMIT 1", (dedupe_key,))
            if r and (dedupe_hours is None or utcnow() - parse_iso(r["ts"]) < timedelta(hours=dedupe_hours)):
                return False
        self.s.x("INSERT INTO notifications(ts,dedupe_key,title,body,priority,tags) VALUES(?,?,?,?,?,?)",
                 (iso(utcnow()), dedupe_key, title, body, priority, tags))
        return True

    def flush(self) -> tuple[int, int]:
        # a message that could not go out within a day is stale; keep it on record, do not push it
        cutoff = iso(utcnow() - timedelta(hours=24))
        self.s.x("UPDATE notifications SET sent=-1, error=COALESCE(error,'expired unsent') "
                 "WHERE sent=0 AND ts<?", (cutoff,))
        pending = self.s.q("SELECT * FROM notifications WHERE sent=0 AND attempts<20 ORDER BY id LIMIT 30")
        sent = failed = 0
        if not self.channels:
            log.warning("no notification channel configured; %d message(s) kept in the database", len(pending))
            return 0, len(pending)
        for n in pending:
            errors = []
            delivered = False
            for ch in self.channels:
                try:
                    (self._ntfy if ch == "ntfy" else self._telegram)(n)
                    delivered = True
                except ProviderError as e:
                    errors.append(str(e))
            if delivered:
                self.s.x("UPDATE notifications SET sent=1, attempts=attempts+1, error=? WHERE id=?",
                         ("; ".join(errors) or None, n["id"]))
                sent += 1
            else:
                self.s.x("UPDATE notifications SET attempts=attempts+1, error=? WHERE id=?",
                         ("; ".join(errors)[:500], n["id"]))
                failed += 1
        return sent, failed

    def _ntfy(self, n):
        # JSON publishing keeps UTF-8 (e.g. the euro sign) intact in titles
        prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(n["priority"] or "default", 3)
        msg = {"topic": self.cfg.ntfy_topic, "title": n["title"], "message": n["body"], "priority": prio}
        if n["tags"]:
            msg["tags"] = [t for t in n["tags"].split(",") if t]
        if self.cfg.dashboard_url:
            msg["click"] = self.cfg.dashboard_url
        headers = {"Content-Type": "application/json"}
        if self.cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {self.cfg.ntfy_token}"
        self.http(f"{self.cfg.ntfy_server}/", method="POST", data=json.dumps(msg).encode("utf-8"),
                  headers=headers, provider="ntfy", retries=2)

    def _telegram(self, n):
        payload = json.dumps({"chat_id": self.cfg.telegram_chat_id,
                              "text": f"{n['title']}\n\n{n['body']}"}).encode()
        self.http(f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage", method="POST",
                  data=payload, headers={"Content-Type": "application/json"}, provider="telegram", retries=2)
