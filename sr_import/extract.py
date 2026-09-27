"""Read-only extractors for the two conversation sources.

Both return the same Record shape so the importer never learns where a line
came from. Nothing here writes anywhere.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config

#: Compaction blocks and host context injections are not conversation. A single
#: <cb_summary> block reached 850k characters on this machine; importing it
#: would be importing the same history dozens of times over.
_NOISE_BLOCKS = re.compile(
    r"<(cb_summary|system-reminder|user_info|user_memory|identity_context|"
    r"project_layout|additional_data|memory_and_skills_reminder|"
    r"wecom_forwarded_chat_archive|available_deferred_tools)\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_SELF_CLOSING = re.compile(r"<[a-zA-Z][\w:-]*(?:\s[^<>]*?)?/>")
_HTML_TAG = re.compile(r"</?[a-zA-Z][\w:-]*(?:\s[^<>]*?)?>")

#: Both hosts store host-generated reminders under role="user", so the role
#: filter alone cannot keep them out. These are deliberately narrow: a false
#: hit loses a real memory, a miss only costs one noisy source. Matched
#: case-insensitively after HTML-entity decoding (QA caught `&quot;` slips).
_NOISE_CONTAINS = (
    "todowrite tool hasn",      # Zcode/Claude Code reminder, stored as user text
    "chatcmpl-tool-",           # background-command tool ids
    "background command",       # WorkBuddy background shell output
    "<system-reminder",         # defense in depth if a new block type appears
    "system reminder",
)
#: A whole message that is just a credential-shaped token. Matches the QA
#: finding "cfat_sm8QRk..." -- a bare token stored as a user turn.
_BARE_TOKEN = re.compile(r"^(?:cfat_|sk-|ghp_|github_pat_|xoxb-)\S+$")
#: Records dropped by the blacklist, exposed for plan/check to report.
NOISE_HITS: dict[str, int] = {}


@dataclass(frozen=True)
class Record:
    key: str
    source: str
    entry: str
    session_id: str
    session_title: str
    cwd: str
    role: str
    origin: str
    content: str
    occurred_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def clean(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = _NOISE_BLOCKS.sub("", text)
    text = _SELF_CLOSING.sub("", text)
    text = _HTML_TAG.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def is_noise(text: str) -> bool:
    """Host-generated reminders that wear a user costume. Narrow by design."""
    lowered = text.lower()
    for needle in _NOISE_CONTAINS:
        if needle in lowered:
            NOISE_HITS[needle] = NOISE_HITS.get(needle, 0) + 1
            return True
    if _BARE_TOKEN.match(text.strip()):
        NOISE_HITS["bare-token"] = NOISE_HITS.get("bare-token", 0) + 1
        return True
    return False


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value, default_ms: int | None = None) -> str:
    """Timestamps arrive as epoch ms, epoch s, or ISO text depending on the source."""
    if isinstance(value, (int, float)) and value > 0:
        ms = float(value)
        if ms > 1e12:  # epoch seconds would be ~1.7e9
            ms = ms
        elif ms > 1e9:
            ms = ms * 1000.0
        else:
            ms = None
        if ms:
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    if default_ms:
        return _iso(default_ms)
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("input_text", "output_text", "text"):
                out.append(block.get("text") or "")
        return "\n".join(out)
    return ""


def workbuddy_records(roles: set[str]) -> Iterator[Record]:
    """CodeBuddy/WorkBuddy stores one JSON event per line under projects/."""
    root = config.WORKBUDDY_PROJECTS
    if not root.exists():
        return
    for path in sorted(root.rglob("*.jsonl")):
        try:
            handle = path.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "message":
                    continue
                role = row.get("role")
                if role not in roles:
                    continue
                text = clean(_text_of(row.get("content")))
                if len(text) < config.MIN_CONTENT_CHARS or is_noise(text):
                    continue
                sid = str(row.get("sessionId") or path.stem)
                yield Record(
                    key=f"wb:{sid}:{row.get('id') or row.get('timestamp')}",
                    source="workbuddy",
                    entry="workbuddy",
                    session_id=sid,
                    session_title=path.parent.name[:80],
                    cwd=str(row.get("cwd") or ""),
                    role=role,
                    origin=config.ORIGIN_FOR_ROLE[role],
                    content=text,
                    occurred_at=_iso(row.get("timestamp")),
                )


def zcode_records(roles: set[str]) -> Iterator[Record]:
    """Zcode keeps an opencode-shaped SQLite: message(role) joined to part(text)."""
    path = config.ZCODE_DB
    if not path.exists():
        return
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT p.data AS part, m.data AS msg, m.id AS mid, m.session_id AS sid,
                   m.time_created AS tc, s.title AS title, s.directory AS directory
            FROM part p
            JOIN message m ON m.id = p.message_id
            LEFT JOIN session s ON s.id = m.session_id
            ORDER BY m.time_created
            """
        )
        for row in rows:
            try:
                part = json.loads(row["part"] or "{}")
                msg = json.loads(row["msg"] or "{}")
            except ValueError:
                continue
            if part.get("type") != "text":
                continue
            role = msg.get("role")
            if role not in roles:
                continue
            text = clean(part.get("text") or "")
            if len(text) < config.MIN_CONTENT_CHARS or is_noise(text):
                continue
            sid = str(row["sid"] or "")
            yield Record(
                key=f"zc:{sid}:{row['mid']}",
                source="zcode",
                entry="zcode",
                session_id=sid,
                session_title=(row["title"] or "")[:80],
                cwd=str(row["directory"] or ""),
                role=role,
                origin=config.ORIGIN_FOR_ROLE[role],
                content=text,
                occurred_at=_iso(row["tc"] or (part.get("time") or {}).get("created")
                                 if isinstance(part.get("time"), dict) else row["tc"]),
            )
    finally:
        con.close()


def dedup(records: list[Record]) -> list[Record]:
    """Same text, same memory: keep the earliest occurrence, drop the echoes.

    Deduplication runs on cleaned text, so two copies differing only in
    whitespace collapse into one.
    """
    best: dict[str, tuple[int, Record]] = {}
    for record in records:
        sha = content_digest(record.content)
        hit = best.get(sha)
        if hit is None:
            best[sha] = (len(best), record)
        elif record.occurred_at < hit[1].occurred_at:
            best[sha] = (hit[0], record)
    return [pair[1] for pair in sorted(best.values(), key=lambda p: p[0])]


def collect(sources: list[str], roles: set[str]) -> list[Record]:
    """Deduplicated snapshot of everything eligible, in first-seen order."""
    return dedup(list(iter_records(sources, roles)))


def iter_records(sources: list[str], roles: set[str]) -> Iterator[Record]:
    for name in sources:
        if name == "workbuddy":
            yield from workbuddy_records(roles)
        elif name == "zcode":
            yield from zcode_records(roles)
        else:
            raise SystemExit("unknown source: %s" % name)
