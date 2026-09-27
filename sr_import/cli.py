"""Command line entry point.

    python3 -m sr_import.cli plan                 # read only, no writes
    python3 -m sr_import.cli register-entries     # idempotent
    python3 -m sr_import.cli run --limit 30       # the PoC
    python3 -m sr_import.cli run                  # full backfill, incremental
    python3 -m sr_import.cli status
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from . import config, extract, importer, ledger

SOURCES = ["workbuddy", "zcode"]


def _cmd_plan(args) -> int:
    roles = set(args.roles.split(","))
    stats = Counter()
    chars = 0
    first = last = None
    sample = []
    for record in extract.collect(args.source, roles):
        stats[(record.source, record.role, record.origin)] += 1
        chars += len(record.content)
        first = record.occurred_at if first is None else min(first, record.occurred_at)
        last = record.occurred_at if last is None else max(last, record.occurred_at)
        if len(sample) < args.sample:
            sample.append(record)
    print("plan (read only, nothing written)")
    print("  roles: %s | min_chars: %d" % (",".join(sorted(roles)), config.MIN_CONTENT_CHARS))
    for key, count in sorted(stats.items()):
        print("  %-10s %-9s %-18s %6d" % (key[0], key[1], key[2], count))
    print("  total %d records, %.1f 万字" % (sum(stats.values()), chars / 1e4))
    if extract.NOISE_HITS:
        print("  noise dropped: %s" % extract.NOISE_HITS)
    print("  time range: %s .. %s" % (first, last))
    for record in sample:
        print("\n--- %s [%s] %s" % (record.key, record.origin, record.occurred_at))
        print("    title: %s" % record.session_title)
        print("    %s" % record.content[:200].replace("\n", " "))
    return 0


def _cmd_register_entries(args) -> int:
    scope_ids = config.shared_scope_ids()
    engine = importer.Importer(scope_ids[0], scope_ids, dry_run=args.dry_run)
    print("entries:", engine.register_entries())
    return 0


def _cmd_run(args) -> int:
    scope_ids = config.shared_scope_ids()
    scope_id = scope_ids[0]
    roles = set(args.roles.split(","))
    con = ledger.connect()
    seen = ledger.known_keys(con)
    run_id = importer.new_run_id()
    ledger.start_run(con, run_id, importer.utc_now(), ",".join(args.source))

    selected = []
    skipped = 0
    for record in extract.collect(args.source, roles):
        if record.key in seen:
            skipped += 1
            continue
        selected.append(record)
        if args.limit and len(selected) >= args.limit:
            break
    print("run %s | scope=%s" % (run_id, scope_id[:60] + "..."))
    print("  planned %d (already imported %d skipped)" % (len(selected), skipped))
    if not selected:
        ledger.finish_run(con, run_id, importer.utc_now(), {"planned": 0}, "nothing new")
        print("  nothing new; store unchanged")
        return 0

    engine = importer.Importer(scope_id, scope_ids, dry_run=args.dry_run)
    stats = Counter()
    rows = []
    ordered = sorted(selected, key=lambda r: r.origin)
    for batch in importer.chunks(ordered, min(args.batch, config.BATCH_LIMIT)):
        outcomes = engine.import_batch(batch)
        for record, outcome in zip(batch, outcomes):
            stats[outcome.disposition] += 1
            rows.append((
                record.key, record.source, record.entry, record.role, record.origin,
                record.session_id, record.occurred_at, importer.utc_now(),
                outcome.error_code or "", outcome.disposition,
            ))
        print("  batch %-3d -> %s" % (len(batch), dict(stats)))
    if not args.dry_run:
        ledger.record(con, rows)
    summary = {"planned": len(selected), "inserted": stats.get("inserted", 0),
               "duplicate": stats.get("duplicate", 0), "rejected": stats.get("rejected", 0)}
    ledger.finish_run(con, run_id, importer.utc_now(), summary, "dry-run" if args.dry_run else "")
    print("  done: %s" % summary)
    return 0


def _cmd_check(args) -> int:
    """Dry-run every record through the real capture validator.

    ``prepare_capture`` applies the secret scanner, the empty-source rule and
    the segment splitter without writing anything, so this predicts exactly
    what the store will accept before we ask it to.
    """
    from scope_recall.contracts import TrustedContext
    from scope_recall.core.events import prepare_capture

    roles = set(args.roles.split(","))
    scope_ids = config.shared_scope_ids()
    agent_id, installation_id = config.binding_fields()
    from scope_recall.contracts import InstanceBinding
    binding = InstanceBinding(agent_id, installation_id, config.SHARED_ROOT,
                              frozenset(scope_ids), False, "shared")
    stats = Counter()
    rejected = []
    checked = 0
    for record in extract.collect(args.source, roles):
        if args.limit and checked >= args.limit:
            break
        checked += 1
        context = TrustedContext(binding, f"sr-import:{record.session_id}"[:240],
                                 frozenset(scope_ids), record.origin, entry_id=record.entry)
        event = importer.Importer._event(record, importer.utc_now())
        try:
            prepared = prepare_capture(event, context)
        except Exception as exc:  # noqa: BLE001 - report, never crash the sweep
            stats["error:%s" % type(exc).__name__] += 1
            continue
        if prepared.rejection:
            stats[prepared.rejection] += 1
            if len(rejected) < 5:
                rejected.append((prepared.rejection, record.key, record.content[:80]))
        else:
            stats["accepted(%d event)" % len(prepared.events)] += 1
    print("check (read only, nothing written)")
    print("  checked %d records" % checked)
    for key, count in stats.most_common():
        print("    %-24s %6d" % (key, count))
    for reason, key, text in rejected:
        print("  rejected: %s | %s | %s" % (reason, key, text.replace("\n", " ")))
    return 0


def _cmd_forget_noise(args) -> int:
    """Delete the noise we already imported, via the official forget use case."""
    hits = importer.noise_event_ids()
    print("noise sources currently in the store: %d" % len(hits))
    by_entry = Counter(h["entry"] for h in hits)
    for entry, count in by_entry.most_common():
        print("    %-10s %6d" % (entry, count))
    for hit in hits[:5]:
        print("  e.g. %s | %s" % (hit["event_id"], hit["preview"].replace("\n", " ")))
    if not hits:
        return 0
    if not args.apply:
        print("dry list only; re-run with --apply to delete them")
        return 0
    scope_ids = config.shared_scope_ids()
    engine = importer.Importer(scope_ids[0], scope_ids)
    results = importer.Forgetter(engine).run(hits, batch=args.batch)
    for r in results:
        print("  forget batch: %s" % r)
    total = sum(r["batch"] for r in results if r["ok"])
    print("deleted %d / %d noise sources" % (total, len(hits)))
    return 0 if total == len(hits) else 1


def _cmd_status(args) -> int:
    con = ledger.connect()
    total = con.execute("SELECT count(*) FROM imported").fetchone()[0]
    print("ledger: %s" % config.LEDGER_DB)
    print("  imported records: %d" % total)
    for row in con.execute("SELECT source,role,origin,count(*) FROM imported GROUP BY source,role,origin"):
        print("    %-10s %-9s %-18s %6d" % row)
    print("  recent runs:")
    for row in ledger.recent_runs(con):
        print("    %s | %s | planned=%s inserted=%s dup=%s rejected=%s %s"
              % (row[0], row[3], row[4], row[5], row[6], row[7], row[8] or ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sr-import")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--source", nargs="+", default=SOURCES, choices=SOURCES)
    p.add_argument("--roles", default="user")
    p.add_argument("--sample", type=int, default=3)
    p.set_defaults(func=_cmd_plan)

    p = sub.add_parser("register-entries")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_register_entries)

    p = sub.add_parser("run")
    p.add_argument("--source", nargs="+", default=SOURCES, choices=SOURCES)
    p.add_argument("--roles", default="user")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--batch", type=int, default=config.BATCH_LIMIT)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("check")
    p.add_argument("--source", nargs="+", default=SOURCES, choices=SOURCES)
    p.add_argument("--roles", default="user")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("forget-noise")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--batch", type=int, default=90)
    p.set_defaults(func=_cmd_forget_noise)

    p = sub.add_parser("status")
    p.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
