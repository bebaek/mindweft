from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from mindweft_client.output import print_json

THREAD_ARCHIVE_SCHEMA = "mindweft.thread-archive"
THREAD_ARCHIVE_CHECKSUM_VERSION = 5
LINEAGE_ARCHIVE_SCHEMA = "mindweft.thread-lineage-archive"
LINEAGE_ARCHIVE_CHECKSUM_VERSION = 2
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read archive file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"archive file '{path}' is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("archive JSON must be an object")
    return verify_archive_payload(payload)


def verify_archive_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema = payload.get("schema")
    if schema == THREAD_ARCHIVE_SCHEMA:
        attachment_count = _verify_thread_archive(payload)
        thread_count = 1
    elif schema == LINEAGE_ARCHIVE_SCHEMA:
        thread_count, attachment_count = _verify_lineage_archive(payload)
    else:
        raise ValueError(f"unsupported archive schema: {schema!r}")
    return {
        "valid": True,
        "schema": schema,
        "version": payload.get("version"),
        "archive_id": _required_string(payload, "archive_id", context="archive"),
        "content_sha256": _required_checksum(payload, context="archive"),
        "thread_count": thread_count,
        "attachment_count": attachment_count,
    }


def _verify_thread_archive(payload: dict[str, Any]) -> int:
    version = payload.get("version")
    if version != THREAD_ARCHIVE_CHECKSUM_VERSION:
        raise ValueError(
            f"thread archive version {version!r} does not provide a whole-content checksum; "
            f"version {THREAD_ARCHIVE_CHECKSUM_VERSION} is required"
        )
    _verify_content_checksum(payload, context="thread archive")
    _required_string(payload, "archive_id", context="thread archive")
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("thread archive thread must be an object")
    _required_string(thread, "source_thread_id", context="thread archive thread")
    if not isinstance(payload.get("context"), dict):
        raise ValueError("thread archive context must be an object")
    if not isinstance(payload.get("messages"), list):
        raise ValueError("thread archive messages must be an array")
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        raise ValueError("thread archive attachments must be an array")
    seen_attachment_ids: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("thread archive attachment entries must be objects")
        attachment_id = _required_string(
            attachment,
            "source_attachment_id",
            context="thread archive attachment",
        )
        if attachment_id in seen_attachment_ids:
            raise ValueError(f"duplicate archive attachment ID '{attachment_id}'")
        seen_attachment_ids.add(attachment_id)
        _verify_attachment(attachment, attachment_id=attachment_id)
    return len(attachments)


def _verify_lineage_archive(payload: dict[str, Any]) -> tuple[int, int]:
    version = payload.get("version")
    if version != LINEAGE_ARCHIVE_CHECKSUM_VERSION:
        raise ValueError(
            f"lineage archive version {version!r} does not provide a whole-content checksum; "
            f"version {LINEAGE_ARCHIVE_CHECKSUM_VERSION} is required"
        )
    _verify_content_checksum(payload, context="lineage archive")
    _required_string(payload, "archive_id", context="lineage archive")
    _required_string(
        payload,
        "requested_source_thread_id",
        context="lineage archive",
    )
    _required_string(payload, "root_source_thread_id", context="lineage archive")
    entries = payload.get("threads")
    if not isinstance(entries, list) or not entries:
        raise ValueError("lineage archive threads must be a non-empty array")
    attachment_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("lineage archive thread entries must be objects")
        nested = entry.get("archive")
        if not isinstance(nested, dict):
            raise ValueError("lineage archive entries must contain an archive object")
        if nested.get("schema") != THREAD_ARCHIVE_SCHEMA:
            raise ValueError("lineage archive entries must contain thread archives")
        attachment_count += _verify_thread_archive(nested)
    return len(entries), attachment_count


def _verify_attachment(attachment: dict[str, Any], *, attachment_id: str) -> None:
    if attachment.get("encoding") != "base64":
        raise ValueError(f"archive attachment '{attachment_id}' encoding must be base64")
    encoded = attachment.get("data")
    if not isinstance(encoded, str):
        raise ValueError(f"archive attachment '{attachment_id}' data must be a string")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"archive attachment '{attachment_id}' data is not valid base64") from exc
    size_bytes = attachment.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError(f"archive attachment '{attachment_id}' size_bytes must be non-negative")
    if len(data) != size_bytes:
        raise ValueError(f"archive attachment '{attachment_id}' size does not match")
    expected = _required_checksum(attachment, context=f"archive attachment '{attachment_id}'")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"archive attachment '{attachment_id}' checksum does not match")


def _verify_content_checksum(payload: dict[str, Any], *, context: str) -> None:
    expected = _required_checksum(payload, context=context)
    checksum_payload = {key: value for key, value in payload.items() if key != "content_sha256"}
    actual = hashlib.sha256(
        json.dumps(
            checksum_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if actual != expected:
        raise ValueError(f"{context} content checksum does not match")


def _required_checksum(payload: dict[str, Any], *, context: str) -> str:
    value = payload.get("content_sha256") if "content_sha256" in payload else payload.get("sha256")
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{context} SHA-256 checksum must be 64 lowercase hexadecimal characters")
    return value


def _required_string(payload: dict[str, Any], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} {key} must be a non-empty string")
    return value
