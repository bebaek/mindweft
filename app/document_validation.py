from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.pdf_validation import PDFValidationError, validate_pdf

TEXT_DOCUMENT_MIME_ALIASES = frozenset({"text/plain", "text/markdown", "text/csv"})

DocumentValidationReason = Literal[
    "unsupported",
    "invalid_utf8",
    "empty_text",
    "nul_text",
    "text_too_large",
    "text_mime_mismatch",
]

DOCUMENT_VALIDATION_MESSAGES: dict[DocumentValidationReason, str] = {
    "unsupported": "unsupported document MIME type",
    "invalid_utf8": "Text document is not valid UTF-8",
    "empty_text": "Text document must not be empty",
    "nul_text": "Text document contains unsupported NUL bytes",
    "text_too_large": "Text document exceeds the maximum allowed size",
    "text_mime_mismatch": "Text document data does not match declared MIME type",
}


class DocumentValidationError(ValueError):
    def __init__(self, reason: DocumentValidationReason) -> None:
        self.reason = reason
        super().__init__(DOCUMENT_VALIDATION_MESSAGES[reason])


@dataclass(frozen=True)
class DocumentMetadata:
    mime_type: str
    page_count: int | None = None
    text: str | None = None


def canonical_document_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized in TEXT_DOCUMENT_MIME_ALIASES:
        return "text/plain"
    return normalized


def validate_document(
    data: bytes,
    mime_type: str,
    *,
    max_pages: int,
    max_text_bytes: int,
) -> DocumentMetadata:
    canonical_mime_type = canonical_document_mime_type(mime_type)
    if canonical_mime_type == "application/pdf":
        try:
            metadata = validate_pdf(data, max_pages=max_pages)
        except PDFValidationError:
            raise
        return DocumentMetadata(mime_type=canonical_mime_type, page_count=metadata.page_count)
    if canonical_mime_type != "text/plain":
        raise DocumentValidationError("unsupported")
    if len(data) > max_text_bytes:
        raise DocumentValidationError("text_too_large")
    if data.startswith(b"%PDF-"):
        raise DocumentValidationError("text_mime_mismatch")
    if b"\x00" in data:
        raise DocumentValidationError("nul_text")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentValidationError("invalid_utf8") from exc
    if not text.strip():
        raise DocumentValidationError("empty_text")
    return DocumentMetadata(mime_type=canonical_mime_type, text=text)
