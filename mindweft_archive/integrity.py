from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

THREAD_ARCHIVE_SCHEMA = "mindweft.thread-archive"
THREAD_ARCHIVE_CHECKSUM_VERSION = 5
LINEAGE_ARCHIVE_SCHEMA = "mindweft.thread-lineage-archive"
LINEAGE_ARCHIVE_CHECKSUM_VERSION = 2
SUPPORTED_THREAD_ARCHIVE_VERSIONS = frozenset({1, 2, 3, 4, 5})
SUPPORTED_LINEAGE_ARCHIVE_VERSIONS = frozenset({1, 2})
CONTENT_SHA256_FIELD = "content_sha256"


def content_sha256(payload: Mapping[str, Any]) -> str:
    """Return the canonical whole-content checksum for a portable archive payload."""
    checksum_payload = {key: value for key, value in payload.items() if key != CONTENT_SHA256_FIELD}
    canonical = json.dumps(
        checksum_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
