from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mindweft_archive import inspect_archive_payload, verify_archive_payload
from mindweft_client.output import print_json


def run_archive_inspect(args: argparse.Namespace, trace_id: str | None) -> int:
    result = inspect_archive_file(Path(args.path))
    if trace_id is not None:
        result["trace_id"] = trace_id
    if args.json:
        print_json(result)
    else:
        fields = [
            f"schema={result['schema']}",
            f"version={result['version']}",
            f"archive_id={result['archive_id']}",
            f"checksum={result['checksum_status']}",
            f"attachment_checksums={result['attachment_checksum_status']}",
            f"threads={result['thread_count']}",
            f"messages={result['message_count']}",
            f"attachments={result['attachment_count']}",
        ]
        for key in (
            "source_thread_id",
            "root_source_thread_id",
            "requested_source_thread_id",
        ):
            value = result.get(key)
            if value is not None:
                fields.append(f"{key}={value}")
        print(" ".join(fields))
        if trace_id is not None:
            print(f"trace_id={trace_id}")
    return 0


def run_archive_verify(args: argparse.Namespace, trace_id: str | None) -> int:
    result = verify_archive_file(Path(args.path))
    if trace_id is not None:
        result["trace_id"] = trace_id
    if args.json:
        print_json(result)
    else:
        print(
            " ".join(
                [
                    "valid=true",
                    f"schema={result['schema']}",
                    f"version={result['version']}",
                    f"archive_id={result['archive_id']}",
                    f"threads={result['thread_count']}",
                    f"attachments={result['attachment_count']}",
                    f"content_sha256={result['content_sha256']}",
                ]
            )
        )
        if trace_id is not None:
            print(f"trace_id={trace_id}")
    return 0


def verify_archive_file(path: Path) -> dict[str, Any]:
    return verify_archive_payload(load_archive_file(path))


def inspect_archive_file(path: Path) -> dict[str, Any]:
    return inspect_archive_payload(load_archive_file(path))


def load_archive_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read archive file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"archive file '{path}' is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("archive JSON must be an object")
    return payload
