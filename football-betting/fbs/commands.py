"""Owner commands delivered as GitHub issues (no token ever leaves the server side).

The app opens a pre-filled "new issue" page. The owner submits it while signed
in to GitHub; the workflow then checks, server-side:

  * the repository is private (a public repo would expose commands and data),
  * the issue author is the repository owner (author_association OWNER),
  * the command is on a fixed allowlist and every field passes strict
    validation. Anything else is refused and nothing runs.

Issue text is never interpolated into a shell; it reaches Python only through
an environment variable and is parsed as data.
"""
from __future__ import annotations

import json
import re

ALLOWED = {"start", "stop", "continue", "confirm", "reject", "settle-bet", "notify-test", "status"}
_OPP = re.compile(r"^OPP-\d{8}-\d{1,3}$")
_REF = re.compile(r"^[A-Za-z0-9 _./:-]{0,64}$")
_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


class CommandError(ValueError):
    pass


def authorize(repo_private: str, author: str, owner: str, association: str) -> None:
    if str(repo_private).lower() != "true":
        raise CommandError("refused: the repository is public; commands run only in a private repository")
    if not author or author.lower() != (owner or "").lower() or association != "OWNER":
        raise CommandError("refused: only the repository owner can issue commands")


def _num(v, lo, hi, name) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise CommandError(f"{name} must be a number") from None
    if not (lo < x <= hi):
        raise CommandError(f"{name} out of range")
    return x


def parse(body: str) -> tuple[str, dict]:
    """Returns (command, validated payload)."""
    m = _BLOCK.search(body or "")
    if not m:
        raise CommandError("no ```json command block found")
    try:
        data = json.loads(m.group(1))
    except ValueError:
        raise CommandError("command block is not valid JSON") from None
    if not isinstance(data, dict):
        raise CommandError("command block must be an object")
    cmd = data.get("command")
    if cmd not in ALLOWED:
        raise CommandError("unknown command")
    out: dict = {}
    if cmd in ("confirm", "reject"):
        oid = str(data.get("id", ""))
        if not _OPP.match(oid):
            raise CommandError("invalid opportunity id")
        out["id"] = oid
    if cmd == "confirm":
        out["stake"] = round(_num(data.get("stake"), 0, 100000, "stake"), 2)
        out["odds"] = round(_num(data.get("odds"), 1.0, 1000, "odds"), 3)
        ref = str(data.get("reference", "") or "")
        if not _REF.match(ref):
            raise CommandError("reference may contain only letters, digits and - _ . / : (max 64)")
        out["reference"] = ref
        out["bookmaker"] = "Bet365"
    if cmd == "continue":
        if data.get("confirm") != "CONTINUE":
            raise CommandError("continue requires confirm = CONTINUE exactly")
        out["confirm"] = "CONTINUE"
    if cmd == "settle-bet":
        out["bet_id"] = int(_num(data.get("bet_id"), 0, 10**9, "bet_id"))
        if data.get("result") not in ("won", "lost", "void"):
            raise CommandError("result must be won, lost or void")
        out["result"] = data["result"]
    return cmd, out
