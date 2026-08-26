from __future__ import annotations

import pytest

from app.document_validation import (
    DocumentMetadata,
    DocumentValidationError,
    DocumentValidationReason,
    canonical_document_mime_type,
    validate_document,
)


def test_canonical_document_mime_type_normalizes_text_aliases_and_parameters() -> None:
    assert canonical_document_mime_type(" Text/Markdown; charset=UTF-8 ") == "text/plain"
    assert canonical_document_mime_type("text/csv") == "text/plain"
    assert canonical_document_mime_type("application/pdf") == "application/pdf"


def test_validate_document_accepts_utf8_text_and_strips_bom_for_provider_text() -> None:
    metadata = validate_document(
        b"\xef\xbb\xbfHello, \xe4\xb8\x96\xe7\x95\x8c!\n",
        "text/plain",
        max_pages=100,
        max_text_bytes=1024,
    )

    assert metadata == DocumentMetadata(mime_type="text/plain", text="Hello, 世界!\n")


@pytest.mark.parametrize(
    ("data", "reason", "message"),
    [
        (b"\xff", "invalid_utf8", "Text document is not valid UTF-8"),
        (b" \n\t", "empty_text", "Text document must not be empty"),
        (b"hello\x00world", "nul_text", "Text document contains unsupported NUL bytes"),
        (b"too large", "text_too_large", "Text document exceeds the maximum allowed size"),
        (
            b"%PDF-1.7",
            "text_mime_mismatch",
            "Text document data does not match declared MIME type",
        ),
    ],
)
def test_validate_document_rejects_invalid_text(
    data: bytes,
    reason: DocumentValidationReason,
    message: str,
) -> None:
    with pytest.raises(DocumentValidationError, match=f"^{message}$") as exc_info:
        validate_document(
            data,
            "text/plain",
            max_pages=100,
            max_text_bytes=4 if reason == "text_too_large" else 1024,
        )

    assert exc_info.value.reason == reason


def test_validate_document_rejects_unsupported_mime_type() -> None:
    with pytest.raises(DocumentValidationError, match="^unsupported document MIME type$"):
        validate_document(
            b"binary",
            "application/octet-stream",
            max_pages=100,
            max_text_bytes=1024,
        )
