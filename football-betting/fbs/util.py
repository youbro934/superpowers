"""HTTP with retries, time helpers. Standard library only."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

log = logging.getLogger("fbs")

USER_AGENT = "fbs-football-model/1.0"


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8-sig", errors="replace")


def http_request(url: str, *, method: str = "GET", params: dict | None = None,
                 headers: dict | None = None, data: bytes | None = None,
                 timeout: float = 25, retries: int = 3, backoff: float = 2.0,
                 provider: str = "http") -> Response:
    """Request with retry on network errors, 429 and 5xx. Raises ProviderError."""
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", errors="replace")
            if e.code == 429 or e.code >= 500:
                last = ProviderError(provider, f"HTTP {e.code}: {body}", e.code)
            else:
                raise ProviderError(provider, f"HTTP {e.code}: {body}", e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = ProviderError(provider, f"network error: {e}")
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    assert last is not None
    raise last


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # tzdata missing
        log.warning("timezone %s unavailable, using UTC", name)
        return timezone.utc


def eur(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}€{cents // 100}.{cents % 100:02d}"
