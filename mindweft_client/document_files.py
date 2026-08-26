from __future__ import annotations

from pathlib import Path

_TEXT_DOCUMENT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".log"})


def read_document_file(raw_path: str) -> tuple[Path, str, bytes]:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError(f"document file not found: {raw_path}")
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError(f"document does not contain PDF data: {raw_path}")
        return path, "application/pdf", data
    if suffix not in _TEXT_DOCUMENT_EXTENSIONS:
        raise ValueError(f"unsupported document file type: {raw_path}")
    if b"\x00" in data:
        raise ValueError(f"text document contains unsupported NUL bytes: {raw_path}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"text document is not valid UTF-8: {raw_path}") from exc
    if not text.strip():
        raise ValueError(f"text document must not be empty: {raw_path}")
    return path, "text/plain", data
