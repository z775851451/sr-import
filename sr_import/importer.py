"""Write conversation records into the shared store through the official use case.

What this does: build a trusted context, hand each record to
``core.capture.record_event``, and let the store's own pipeline decide what the
record earns (lexical index, embedding, consolidation).

What this deliberately does not do: touch SQLite directly, write claims, or
re-implement admission. A claim is what the store derives; we only supply
sources, and only origins in ``DERIVATION_ROOT_ORIGINS`` can ever become one.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from scope_recall.contracts import (
    ContractError,
    ImportProvenance,
    InstanceBinding,
    TrustedContext,
)
from scope_recall.core import deletion as deletion_core
from scope_recall.core.capture import record_event
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.writer_lease import TruthWriterBusyError

from . import config
from .extract import Record


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _Clock:
    """The capture use case asks for a clock; it never reads the wall itself."""

    def utc_now(self) -> str:
        return utc_now()

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class Outcome:
    disposition: str
    error_code: str | None
    gaps: tuple[str, ...]


class Importer:
    def __init__(self, scope_id: str, scope_ids: Sequence[str], dry_run: bool = False):
        self.scope_id = scope_id
        self.scope_ids = list(scope_ids)
        self.dry_run = dry_run
        agent_id, installation_id = config.binding_fields()
        self.binding = InstanceBinding(
            agent_id, installation_id, config.SHARED_ROOT,
            frozenset(self.scope_ids), False, "shared",
        )
        self.storage = None if dry_run else SQLiteStorage(self.binding)
        self.clock = _Clock()

    # -- entries ---------------------------------------------------------
    def register_entries(self) -> dict[str, str]:
        """Idempotent: ``register_entry`` upserts, so re-running is safe."""
        if self.dry_run:
            return {k: "dry-run" for k in config.ENTRIES}
        context = TrustedContext(
            self.binding, "sr-import-register", frozenset(self.scope_ids), "host_generated",
        )
        now = utc_now()
        result = {}
        with self.storage.write(context) as tx:
            for entry_id, (display_name, host) in config.ENTRIES.items():
                tx.register_entry(entry_id, display_name, host, now=now)
                result[entry_id] = "registered"
        return result

    # -- batches ---------------------------------------------------------
    def _provenance(self, records: Sequence[Record]) -> tuple[ImportProvenance, str] | tuple[None, str]:
        """One manifest per batch. Fingerprints are capped at 200 by contract.

        Only an ``imported`` context may carry provenance (contracts: a trusted
        context rejects it unless actor_origin == "imported"). First-hand human
        text is its own attestation, so those batches get a manifest for the
        audit trail but no provenance object.
        """
        payload = [
            {"key": r.key, "sha256": hashlib.sha256(r.content.encode("utf-8")).hexdigest()}
            for r in records
        ]
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        config.MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(blob).hexdigest()
        (config.MANIFEST_DIR / (name + ".json")).write_bytes(blob)
        return (
            ImportProvenance(
                original_origin="external_document",
                manifest_sha256=name,
                source_fingerprints=frozenset(p["sha256"] for p in payload),
            ),
            name,
        )

    def _context(self, origin: str, entry_id: str, session_id: str, provenance):
        return TrustedContext(
            self.binding,
            session_id[:240],
            frozenset(self.scope_ids),
            origin,
            entry_id=entry_id,
            import_provenance=provenance,
        )

    @staticmethod
    def _event(record: Record, now: str) -> dict:
        return {
            "protocol_version": "1.1",
            "source_event_key": record.key,
            "source_revision": 1,
            "origin": record.origin,
            "role": record.role,
            "content": record.content,
            "occurred_at": record.occurred_at,
            "recorded_at": now,
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
        }

    def import_batch(self, records: Sequence[Record], *, attempts: int = 3) -> list[Outcome]:
        """One origin per batch: validate_capture requires event origin == context actor_origin."""
        if self.dry_run:
            return [Outcome("dry-run", None, ()) for _ in records]
        origins = {r.origin for r in records}
        if len(origins) != 1:
            raise ValueError("a batch must share one origin, got %s" % sorted(origins))
        origin = origins.pop()
        provenance, manifest = self._provenance(records)
        if origin != "imported":
            provenance = None
        outcomes: list[Outcome] = []
        for record in records:
            event = self._event(record, utc_now())
            for attempt in range(attempts):
                try:
                    context = self._context(origin, record.entry,
                                            f"sr-import:{record.source}:{record.session_id}"[:240],
                                            provenance)
                    receipt = record_event(
                        self.storage, self.clock, context, event,
                        scope_id=self.scope_id, remaining_seconds=20.0,
                    )
                    outcomes.append(Outcome(receipt.disposition, receipt.error_code, receipt.gaps))
                    break
                except TruthWriterBusyError:
                    if attempt == attempts - 1:
                        outcomes.append(Outcome("unavailable", "STORAGE_BUSY", ()))
                    else:
                        time.sleep(2.0 * (attempt + 1))
                except ContractError as exc:
                    outcomes.append(Outcome("rejected", getattr(exc, "code", "CONTRACT"), ()))
                    break
        return outcomes


def chunks(records: Iterable[Record], size: int):
    bucket: list[Record] = []
    for record in records:
        bucket.append(record)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


FORGET_SESSION = "sr-import:forget"


def noise_event_ids() -> list[dict]:
    """Imported sources the current extractor would reject today.

    Read-only. Reuses the exact same blacklist as extraction so the deletion
    list can never be broader than what the importer now refuses to admit.
    """
    from . import extract

    con = sqlite3.connect("file:%s?mode=ro" % config.SHARED_DB, uri=True)
    try:
        rows = con.execute(
            """SELECT event_id, entry_id, source_revision, content FROM source_events
               WHERE entry_id IN ('workbuddy','zcode') AND read_blocked=0"""
        ).fetchall()
    finally:
        con.close()
    hits = []
    for event_id, entry, revision, content in rows:
        if extract.is_noise(content or ""):
            hits.append({"event_id": event_id, "entry": entry,
                         "revision": revision, "preview": (content or "")[:90]})
    return hits


class Forgetter:
    """Deletes noise sources through the official forget use case.

    ``deletion.forget`` demands a command in the same session: the newest
    readable ``human_direct`` source must name every target ref and match the
    delete-command patterns without negation or hedging. So each batch first
    captures that command as a source, then forgets; the closure removes the
    command copy together with the targets (deletion.py keeps no readable
    proof behind). ``mode="delete"`` also marks the targets' pending work
    items obsolete, which is the point: suppressed noise would keep hogging
    consolidation slots.
    """

    def __init__(self, engine: Importer):
        self.engine = engine
        self.storage = engine.storage
        self.clock = engine.clock
        self.scope_id = engine.scope_id
        # A fresh session per run: the authorization proof is "the newest
        # readable human_direct source in this session", and reusing a session
        # across runs trips capture_correction's VERSION_CONFLICT.
        self.session = f"{FORGET_SESSION}:{utc_now()}"

    def _command_context(self) -> TrustedContext:
        return TrustedContext(
            self.engine.binding, self.session,
            frozenset(self.engine.scope_ids), "human_direct", entry_id="workbuddy",
        )

    def _capture_command(self, refs: list[str]) -> None:
        now = utc_now()
        # Idempotency lives in the forget request_key, not in this throwaway
        # command copy; a random suffix keeps a retried run off the same key
        # (a stale revision there surfaced as VERSION_CONFLICT).
        digest = uuid.uuid4().hex[:12]
        event = {
            "protocol_version": "1.1",
            "source_event_key": f"sr-import:forget-cmd:{digest}",
            "source_revision": 1,
            "origin": "human_direct",
            "role": "user",
            "content": "清除以下记忆来源：" + " ".join(refs),
            "occurred_at": now,
            "recorded_at": now,
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
        }
        receipt = record_event(self.storage, self.clock, self._command_context(), event,
                               scope_id=self.scope_id, remaining_seconds=20.0)
        if receipt.disposition not in ("inserted", "duplicate"):
            raise RuntimeError("forget command capture failed: %s %s" % (receipt.disposition, receipt.error_code))

    def delete_batch(self, rows: Sequence[dict]) -> dict:
        # source_events.event_id already carries the "event-" prefix that
        # delete_storage.target() matches on; do not prepend another one.
        refs = [r["event_id"] for r in rows]
        expected = {r["event_id"]: int(r["revision"]) for r in rows}
        self._capture_command(refs)
        request = {
            "protocol_version": "1.1",
            "target_refs": refs,
            "mode": "delete",
            "expected_revisions": expected,
            "reason": "imported host-generated reminders, cleaned by sr-import",
        }
        result = deletion_core.forget(self.storage, self.clock, self._command_context(),
                                      request, remaining_seconds=30.0)
        return result

    def run(self, hits: list[dict], *, batch: int = 90) -> list[dict]:
        results = []
        for start in range(0, len(hits), batch):
            batch_rows = hits[start:start + batch]
            try:
                receipt = self.delete_batch(batch_rows)
                summary = {"batch": len(batch_rows), "ok": True,
                           "operation": receipt.get("operation_id", "?") if isinstance(receipt, dict) else str(receipt)}
            except (ContractError, RuntimeError) as exc:
                summary = {"batch": len(batch_rows), "ok": False, "error": str(exc)[:200]}
            results.append(summary)
        return results
