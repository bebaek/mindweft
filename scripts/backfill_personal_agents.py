#!/usr/bin/env python3
"""Backfill the default personal assistant in an admin SQLite store."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass

from app.admin_store import SQLiteTenantConfigStore, UserExecutionConfigConflictError
from app.user_execution import ensure_default_personal_agent, validate_user_execution_config
from mindweft_config.unified_config import preferred_mindweft_env


@dataclass
class Summary:
    scanned: int = 0
    updated: int = 0
    already_default: int = 0
    invalid: int = 0
    conflicts: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=preferred_mindweft_env("ADMIN_DB_PATH"))
    parser.add_argument("--encryption-key-env", default="MINDWEFT_ADMIN_ENCRYPTION_KEY")
    parser.add_argument("--tenant-id")
    parser.add_argument("--user-id")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-conflicts", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _list_records(store: SQLiteTenantConfigStore, args: argparse.Namespace):
    list_method = getattr(store, "list_user_execution_configs", None)
    if callable(list_method):
        offset = 0
        while True:
            records, total = list_method(
                tenant_id=args.tenant_id,
                user_id=args.user_id,
                limit=args.batch_size,
                offset=offset,
            )
            if not records:
                return
            yield from records
            offset += len(records)
            if offset >= total:
                return
        return

    # Compatibility path for a script copied into a pod running an older image.
    clauses: list[str] = []
    values: list[str] = []
    if args.tenant_id is not None:
        clauses.append("tenant_id = ?")
        values.append(args.tenant_id)
    if args.user_id is not None:
        clauses.append("user_id = ?")
        values.append(args.user_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with sqlite3.connect(store.db_path) as connection:
        pairs = connection.execute(
            f"SELECT tenant_id, user_id FROM user_execution_configs{where} ORDER BY tenant_id, user_id",
            values,
        ).fetchall()
    for tenant_id, user_id in pairs:
        record = store.get_user_execution_config(tenant_id, user_id)
        if record is not None:
            yield record


def run(args: argparse.Namespace) -> int:
    if not args.db_path:
        print("error: --db-path or MINDWEFT_ADMIN_DB_PATH is required", file=sys.stderr)
        return 2
    if args.batch_size < 1 or args.max_conflicts < 0:
        print(
            "error: batch size must be positive and max conflicts cannot be negative",
            file=sys.stderr,
        )
        return 2

    encryption_key = (
        preferred_mindweft_env("ADMIN_ENCRYPTION_KEY")
        if args.encryption_key_env == "MINDWEFT_ADMIN_ENCRYPTION_KEY"
        else os.getenv(args.encryption_key_env)
    )
    store = SQLiteTenantConfigStore(args.db_path, encryption_key=encryption_key)
    summary = Summary()
    for record in _list_records(store, args):
        summary.scanned += 1
        report = validate_user_execution_config(record.config)
        if not report.valid or report.config is None:
            summary.invalid += 1
            print(
                json.dumps(
                    {
                        "event": "invalid",
                        "tenant_id": record.tenant_id,
                        "user_id": record.user_id,
                        "errors": report.errors,
                    }
                ),
                file=sys.stderr,
            )
            continue
        updated = ensure_default_personal_agent(report.config)
        payload = updated.model_dump(mode="json", exclude_none=True)
        if payload == record.config:
            summary.already_default += 1
            continue
        if args.dry_run:
            summary.updated += 1
            continue
        try:
            store.upsert_user_execution_config(
                record.tenant_id,
                record.user_id,
                payload,
                expected_version=record.version,
            )
            summary.updated += 1
        except UserExecutionConfigConflictError:
            summary.conflicts += 1
            print(
                json.dumps(
                    {
                        "event": "conflict",
                        "tenant_id": record.tenant_id,
                        "user_id": record.user_id,
                    }
                ),
                file=sys.stderr,
            )

    result = {"dry_run": args.dry_run, **summary.__dict__}
    print(json.dumps(result, sort_keys=True))
    return 1 if summary.invalid or summary.conflicts > args.max_conflicts else 0


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
