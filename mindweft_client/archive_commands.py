from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from mindweft_archive import (
    LINEAGE_ARCHIVE_CHECKSUM_VERSION,
    LINEAGE_ARCHIVE_SCHEMA,
    SUPPORTED_LINEAGE_ARCHIVE_VERSIONS,
    SUPPORTED_THREAD_ARCHIVE_VERSIONS,
    THREAD_ARCHIVE_CHECKSUM_VERSION,
    THREAD_ARCHIVE_SCHEMA,
    content_sha256,
)
from mindweft_client.output import print_json

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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


def inspect_archive_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema = payload.get("schema")
    if schema == THREAD_ARCHIVE_SCHEMA:
        return _inspect_thread_archive(payload)
    if schema == LINEAGE_ARCHIVE_SCHEMA:
        return _inspect_lineage_archive(payload)
    raise ValueError(f"unsupported archive schema: {schema!r}")


def verify_archive_payload(payload: dict[str, Any]) -> dict[str, Any]:
    inspection = inspect_archive_payload(payload)
    if inspection["checksum_status"] != "valid":
        schema = inspection["schema"]
        version = inspection["version"]
        required_version = (
            THREAD_ARCHIVE_CHECKSUM_VERSION
            if schema == THREAD_ARCHIVE_SCHEMA
            else LINEAGE_ARCHIVE_CHECKSUM_VERSION
        )
        archive_kind = "thread" if schema == THREAD_ARCHIVE_SCHEMA else "lineage"
        raise ValueError(
            f"{archive_kind} archive version {version!r} does not provide a whole-content "
            f"checksum; version {required_version} is required"
        )
    return {
        "valid": True,
        "schema": inspection["schema"],
        "version": inspection["version"],
        "archive_id": inspection["archive_id"],
        "content_sha256": inspection["content_sha256"],
        "thread_count": inspection["thread_count"],
        "attachment_count": inspection["attachment_count"],
    }


def _inspect_thread_archive(payload: dict[str, Any]) -> dict[str, Any]:
    version = payload.get("version")
    if version not in SUPPORTED_THREAD_ARCHIVE_VERSIONS:
        raise ValueError(f"unsupported thread archive version: {version!r}")
    archive_id = _required_string(payload, "archive_id", context="thread archive")
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("thread archive thread must be an object")
    source_thread_id = _required_string(
        thread,
        "source_thread_id",
        context="thread archive thread",
    )
    if not isinstance(payload.get("context"), dict):
        raise ValueError("thread archive context must be an object")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("thread archive messages must be an array")
    if version == 1:
        if "attachments" in payload:
            raise ValueError("thread archive version 1 must not contain attachments")
        attachment_count = 0
        attachment_checksum_status = "unavailable"
    else:
        attachment_count = _verify_attachment_manifest(payload)
        attachment_checksum_status = "valid"
    if version == THREAD_ARCHIVE_CHECKSUM_VERSION:
        _verify_content_checksum(payload, context="thread archive")
        checksum_status = "valid"
        content_sha256: str | None = _required_checksum(payload, context="thread archive")
    else:
        checksum_status = "unavailable"
        content_sha256 = None
    return {
        "schema": THREAD_ARCHIVE_SCHEMA,
        "version": version,
        "archive_id": archive_id,
        "checksum_status": checksum_status,
        "attachment_checksum_status": attachment_checksum_status,
        "content_sha256": content_sha256,
        "thread_count": 1,
        "message_count": len(messages),
        "attachment_count": attachment_count,
        "source_thread_id": source_thread_id,
    }


def _inspect_lineage_archive(payload: dict[str, Any]) -> dict[str, Any]:
    version = payload.get("version")
    if version not in SUPPORTED_LINEAGE_ARCHIVE_VERSIONS:
        raise ValueError(f"unsupported lineage archive version: {version!r}")
    archive_id = _required_string(payload, "archive_id", context="lineage archive")
    requested_source_thread_id = _required_string(
        payload,
        "requested_source_thread_id",
        context="lineage archive",
    )
    root_source_thread_id = _required_string(
        payload,
        "root_source_thread_id",
        context="lineage archive",
    )
    entries = payload.get("threads")
    if not isinstance(entries, list) or not entries:
        raise ValueError("lineage archive threads must be a non-empty array")
    expected_nested_version = 5 if version == 2 else 4
    message_count = 0
    attachment_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("lineage archive thread entries must be objects")
        nested = entry.get("archive")
        if not isinstance(nested, dict) or nested.get("schema") != THREAD_ARCHIVE_SCHEMA:
            raise ValueError("lineage archive entries must contain thread archives")
        if nested.get("version") != expected_nested_version:
            raise ValueError(
                f"lineage archive version {version} requires nested thread archive "
                f"version {expected_nested_version}"
            )
        nested_result = _inspect_thread_archive(nested)
        message_count += int(nested_result["message_count"])
        attachment_count += int(nested_result["attachment_count"])
    if version == LINEAGE_ARCHIVE_CHECKSUM_VERSION:
        _verify_content_checksum(payload, context="lineage archive")
        checksum_status = "valid"
        content_sha256: str | None = _required_checksum(payload, context="lineage archive")
    else:
        checksum_status = "unavailable"
        content_sha256 = None
    return {
        "schema": LINEAGE_ARCHIVE_SCHEMA,
        "version": version,
        "archive_id": archive_id,
        "checksum_status": checksum_status,
        "attachment_checksum_status": "valid",
        "content_sha256": content_sha256,
        "thread_count": len(entries),
        "message_count": message_count,
        "attachment_count": attachment_count,
        "root_source_thread_id": root_source_thread_id,
        "requested_source_thread_id": requested_source_thread_id,
    }


def _verify_attachment_manifest(payload: dict[str, Any]) -> int:
    attachments = payload.get("attachments", [])
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
    if content_sha256(payload) != expected:
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
