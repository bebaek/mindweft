from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.pdf_validation import PDFMetadata, PDFValidationError, validate_pdf


def pdf_bytes(*, pages: int = 1, encrypted: bool = False) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("secret")
    writer.write(output)
    return output.getvalue()


def test_validate_pdf_accepts_valid_document_and_reports_page_count() -> None:
    assert validate_pdf(pdf_bytes(pages=2), max_pages=10) == PDFMetadata(page_count=2)


@pytest.mark.parametrize(
    "data",
    [
        b"not a PDF",
        b"%PDF-invalid",
        b"%PDF-1.7\nstartxref\n0\n%%EOF",
        pdf_bytes()[:40],
    ],
)
def test_validate_pdf_rejects_malformed_data_without_parser_details(data: bytes) -> None:
    with pytest.raises(PDFValidationError, match="^PDF document is malformed$") as exc_info:
        validate_pdf(data, max_pages=10)

    assert exc_info.value.reason == "malformed"
    assert "xref" not in str(exc_info.value).lower()


def test_validate_pdf_rejects_encrypted_documents() -> None:
    with pytest.raises(
        PDFValidationError,
        match="^Encrypted PDF documents are not supported$",
    ) as exc_info:
        validate_pdf(pdf_bytes(encrypted=True), max_pages=10)

    assert exc_info.value.reason == "encrypted"


def test_validate_pdf_rejects_empty_documents() -> None:
    with pytest.raises(
        PDFValidationError,
        match="^PDF document must contain at least one page$",
    ) as exc_info:
        validate_pdf(pdf_bytes(pages=0), max_pages=10)

    assert exc_info.value.reason == "empty"


def test_validate_pdf_enforces_page_limit() -> None:
    with pytest.raises(
        PDFValidationError,
        match="^PDF document exceeds the maximum allowed page count$",
    ) as exc_info:
        validate_pdf(pdf_bytes(pages=3), max_pages=2)

    assert exc_info.value.reason == "too_many_pages"
