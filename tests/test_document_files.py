from pathlib import Path

import pytest

from mindweft_client.document_files import read_document_file


@pytest.mark.parametrize("suffix", [".txt", ".md", ".csv", ".log"])
def test_read_document_file_accepts_utf8_text_extensions(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"notes{suffix}"
    data = "Hello, 世界!\n".encode()
    path.write_bytes(data)

    resolved, mime_type, content = read_document_file(str(path))

    assert resolved == path
    assert mime_type == "text/plain"
    assert content == data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\xff", "not valid UTF-8"),
        (b" \n", "must not be empty"),
        (b"hello\x00world", "unsupported NUL bytes"),
    ],
)
def test_read_document_file_rejects_invalid_text(
    tmp_path: Path,
    data: bytes,
    message: str,
) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(data)

    with pytest.raises(ValueError, match=message):
        read_document_file(str(path))


def test_read_document_file_rejects_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "notes.docx"
    path.write_bytes(b"not really docx")

    with pytest.raises(ValueError, match="unsupported document file type"):
        read_document_file(str(path))
