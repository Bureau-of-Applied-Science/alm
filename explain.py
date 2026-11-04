"""What a log entry is and why it is there: signatures, stored explanations, lookup.

An entry's *signature* is its source plus its message with the parts that vary (addresses, numbers, user
names, paths) replaced by placeholders, so every `block TCP 1.2.3.4 -> :22` collapses into one kind of
thing. An explanation is written once per signature (by a Claude pass, see tools/explain.py) and stored in
the history database; the detail view looks it up. The varying parts come back as `params`, so the stored
text can say `{ip}` or `{path}` and read as if it were about this very entry.

Seam for later: `lookup` is the one place that knows a signature has no explanation yet. An API-backed
generator would be called there and its answer stored with `History.put_explanation(..., by="api")`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

CATALOGUE = Path(__file__).resolve().parent / "knowledge" / "explanations.json"

SIG_MAX = 160

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])")
_MAC = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
_ISO = re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?")
_HEX = re.compile(r"\b(?:0x[0-9a-fA-F]+|[0-9a-f]{8,})\b")
_USER_AFTER_USER = re.compile(r"\b(invalid user|for user|user) (?!<)(\S+)")
_USER_FOR_FROM = re.compile(r"\bfor (?!<|user\b|invalid\b)(\S+) from\b")
_NUM = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?:ms|s|h|min|m|x|%|[KMG]B)?(?![A-Za-z0-9_])")
_SUDO = re.compile(r"^\s*(?P<user>\S+) : (?:TTY=\S+ ; )?PWD=(?P<pwd>\S+) ; USER=(?P<as>\S+) ; COMMAND=(?P<cmd>.*)$")
_CADDY_OLD = re.compile(r"^GET (?P<path>\S+) -> (?P<status>\d+)\s+(?P<ip>\S+)$")
_CADDY = re.compile(r"^(?P<who>.+?) (?P<verb>probed|sent [A-Z]+ to|requested|was redirected at|loaded|viewed) "
                    r"(?P<path>\S+)(?: \((?P<note>[^)]*)\))? from (?P<ip>\S+)$")


class _Params:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._count: dict[str, int] = {}

    def add(self, kind: str, value: str) -> str:
        n = self._count[kind] = self._count.get(kind, 0) + 1
        self.values[kind if n == 1 else f"{kind}{n}"] = value
        return f"<{kind}>"


def signature(source: str, message: str) -> tuple[str, dict[str, str]]:
    """(signature, params). The signature is stable across addresses, numbers, names and paths."""
    p = _Params()
    m = _CADDY.match(message) if source == "caddy" else None
    if m:
        who, verb, note = m["who"], m["verb"], m["note"]
        p.add("who", who); p.add("path", m["path"]); p.add("ip", m["ip"])
        if note:
            p.values["status"] = note
        sig = f"caddy|<who> {verb} <path>" + (f" ({note})" if note else "") + " from <ip>"
        return sig, p.values
    m = _CADDY_OLD.match(message) if source == "caddy" else None
    if m:
        p.add("path", m["path"]); p.add("status", m["status"]); p.add("ip", m["ip"])
        return "caddy|GET <path> -> <status> <ip>", p.values
    if source == "caddy" and " · " in message:
        head, _, subject = message.partition(" · ")
        p.add("subject", subject)
        s = _NUM.sub(lambda x: p.add("n", x.group()), head)
        return f"caddy|{s[:SIG_MAX]}", p.values
    if source == "caddy" and message.startswith("{"):
        return "caddy|raw access-log JSON", {}
    m = _SUDO.match(message) if source == "sudo" else None
    if m:
        p.add("user", m["user"]); p.add("pwd", m["pwd"]); p.add("as", m["as"]); p.add("cmd", m["cmd"][:200])
        return "sudo|<user> ran a command as <as>", p.values
    s = message
    s = re.sub(r"(?<=run-u)\d+", "<n>", s)
    s = re.sub(r"(?<=SHA256:)\S+", lambda x: p.add("key", x.group()), s)
    s = _ISO.sub(lambda x: p.add("ts", x.group()), s)
    s = _MAC.sub(lambda x: p.add("mac", x.group()), s)
    s = _IPV4.sub(lambda x: p.add("ip", x.group()), s)
    s = _IPV6.sub(lambda x: p.add("ip", x.group()) if "::" in x.group() or x.group().count(":") >= 5 else x.group(), s)
    s = _USER_AFTER_USER.sub(lambda x: f"{x.group(1)} {p.add('user', x.group(2))}", s)
    s = _USER_FOR_FROM.sub(lambda x: f"for {p.add('user', x.group(1))} from", s)
    s = _HEX.sub(lambda x: p.add("hex", x.group()), s)
    s = _NUM.sub(lambda x: p.add("n", x.group()), s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(",")
    return f"{source}|{s[:SIG_MAX]}", p.values


def fill(text: str, params: dict[str, str]) -> str:
    """Replace {ip}, {path}, {n2}... with this entry's values; unknown names become `?`."""
    return re.sub(r"\{(\w+)\}", lambda x: params.get(x.group(1), "?"), text)


def lookup(history, source: str, message: str) -> dict:
    """The explanation for one entry, ready to show. `found` is False when nobody has written one yet."""
    sig, params = signature(source, message)
    row = history.explanation(sig) if history is not None else None
    if not row:
        return {"found": False, "sig": sig}
    return {"found": True, "sig": sig, "category": row["category"], "title": fill(row["title"], params),
            "what": fill(row["what"], params), "why": fill(row["why"], params), "by": row["by"]}


def load_catalogue(history, path: Path = CATALOGUE) -> int:
    """Put the shipped explanations (knowledge/explanations.json) into the history database, so the inspect view
    works on a fresh install. Only signatures the database does not have yet are added, so local edits and
    anything written with tools/explain.py survive. Returns how many were added."""
    if history is None:
        return 0
    try:
        rows = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return 0
    added = 0
    for r in rows:
        if history.explanation(r["sig"]) is None:
            history.put_explanation(r["sig"], r["source"], r["category"], r["title"], r["what"], r["why"],
                                    r.get("by", "claude"))
            added += 1
    return added
