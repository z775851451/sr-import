"""Paths and constants for the Scope Recall backfill tool.

Everything here is read-only configuration. No secrets, no writes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))

#: The shared store every agent attaches to. Never open its SQLite directly.
SHARED_ROOT = HOME / "ScopeRecall" / "shared"
SHARED_DB = SHARED_ROOT / "memory.sqlite3"
RUNTIME_CONFIG = SHARED_ROOT / "runtime-config.json"

#: WorkBuddy conversation logs (JSONL, one event per line).
WORKBUDDY_PROJECTS = HOME / ".workbuddy" / "projects"

#: Zcode / opencode-style store (SQLite).
ZCODE_DB = HOME / ".zcode" / "cli" / "db" / "db.sqlite"

#: Tool-owned state. Deliberately outside the shared store.
TOOL_DIR = Path(__file__).resolve().parent.parent
LEDGER_DB = TOOL_DIR / "state" / "imported.sqlite3"
MANIFEST_DIR = TOOL_DIR / "state" / "manifests"
LOG_DIR = TOOL_DIR / "state" / "logs"

#: Entry ids registered in the shared store. Must match ENTRY_ID = [a-z][a-z0-9-]{1,31}
ENTRIES = {
    "workbuddy": ("WorkBuddy", "workbuddy"),
    "zcode": ("Zcode", "zcode"),
}

#: Origin -> role mapping. Only these three origins are derivation roots
#: (core/evidence_question.py DERIVATION_ROOT_ORIGINS), so anything else we
#: import can never become a claim.
ORIGIN_FOR_ROLE = {
    "user": "human_direct",
    "assistant": "assistant_visible",
}

#: A distilled summary is second-hand: importable, but it will qualify as
#: proposed (a lead), never promoted to active without a human restating it.
DISTILLED_ORIGIN = "imported"

MIN_CONTENT_CHARS = 30

#: Import provenance accepts 1..200 source fingerprints per batch.
BATCH_LIMIT = 200


def load_runtime_config() -> dict:
    return json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))


def shared_scope_ids(cfg: dict | None = None) -> list[str]:
    """The scopes this store allows, owner-private first (every agent reads it)."""
    cfg = cfg or load_runtime_config()
    scopes = list(cfg.get("binding", {}).get("scope_ids") or [])
    if not scopes:
        raise SystemExit("runtime-config has no binding.scope_ids; refusing to guess")
    owner = [s for s in scopes if s.startswith("audience:13:owner_private")]
    return owner + [s for s in scopes if s not in owner]


def binding_fields(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or load_runtime_config()
    b = cfg.get("binding", {})
    return b["agent_id"], b["installation_id"]
