from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from pypdf import PdfReader
from pypdf.errors import PdfReadError

PDFValidationReason = Literal["malformed", "encrypted", "empty", "too_many_pages"]

PDF_VALIDATION_MESSAGES: dict[PDFValidationReason, str] = {
    "malformed": "PDF document is malformed",
    "encrypted": "Encrypted PDF documents are not supported",
    "empty": "PDF document must contain at least one page",
    "too_many_pages": "PDF document exceeds the maximum allowed page count",
}


class PDFValidationError(ValueError):
    def __init__(self, reason: PDFValidationReason) -> None:
        self.reason = reason
        super().__init__(PDF_VALIDATION_MESSAGES[reason])


@dataclass(frozen=True)
class PDFMetadata:
    page_count: int


def validate_pdf(data: bytes, *, max_pages: int) -> PDFMetadata:
    """Validate bounded PDF bytes without exposing parser diagnostics."""
    if not data.startswith(b"%PDF-"):
        raise PDFValidationError("malformed")
    tail = data[-65_536:]
    if b"startxref" not in tail or b"%%EOF" not in tail:
        raise PDFValidationError("malformed")

    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise PDFValidationError("encrypted")
        page_count = len(reader.pages)
    except PDFValidationError:
        raise
    except (PdfReadError, OSError, TypeError, ValueError, KeyError, AssertionError) as exc:
        raise PDFValidationError("malformed") from exc

    if page_count == 0:
        raise PDFValidationError("empty")
    if page_count > max_pages:
        raise PDFValidationError("too_many_pages")
    return PDFMetadata(page_count=page_count)
