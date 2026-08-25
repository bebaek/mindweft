from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from app.message_parts import (
    attachment_ids,
    is_attachment_part,
    message_attachment_parts,
    remap_attachment_ids,
)
from app.models import (
    AttachmentPartBase,
    ImagePart,
    Message,
    MessageRole,
    TextPart,
)


class DocumentPartForTest(AttachmentPartBase):
    type: Literal["document"] = "document"
    label: str


def test_image_part_wire_shape_is_unchanged() -> None:
    part = ImagePart(
        mime_type="image/png",
        attachment_id="attachment-1",
        detail="high",
    )

    assert isinstance(part, AttachmentPartBase)
    assert is_attachment_part(part)
    assert part.model_dump(mode="json") == {
        "mime_type": "image/png",
        "data": None,
        "url": None,
        "attachment_id": "attachment-1",
        "type": "image",
        "detail": "high",
    }


def test_message_part_discriminator_still_rejects_unknown_types() -> None:
    with pytest.raises(ValidationError):
        Message(
            thread_id="thread-1",
            role=MessageRole.USER,
            content="unsupported",
            parts=[{"type": "document", "mime_type": "application/pdf"}],  # type: ignore[list-item]
        )


def test_attachment_ids_preserve_message_order_and_duplicates() -> None:
    first = Message(
        thread_id="thread-1",
        role=MessageRole.USER,
        content="first",
        parts=[
            TextPart(text="first"),
            ImagePart(mime_type="image/png", attachment_id="attachment-1"),
            ImagePart(mime_type="image/png", data="aW5saW5l"),
        ],
    )
    second = Message(
        thread_id="thread-1",
        role=MessageRole.USER,
        content="second",
        parts=[
            ImagePart(mime_type="image/png", attachment_id="attachment-1"),
            ImagePart(mime_type="image/png", attachment_id="attachment-2"),
            ImagePart(mime_type="image/png", url="https://example.test/image.png"),
        ],
    )

    assert attachment_ids([first, second]) == [
        "attachment-1",
        "attachment-1",
        "attachment-2",
    ]
    assert first.parts is not None
    assert message_attachment_parts(first) == first.parts[1:]


def test_remap_attachment_ids_preserves_subtype_fields_without_mutating_source() -> None:
    source = ImagePart(
        mime_type="image/png",
        attachment_id="attachment-1",
        detail="high",
    )

    remapped = remap_attachment_ids([source], {"attachment-1": "attachment-2"})

    assert remapped is not None
    assert remapped[0] is not source
    assert isinstance(remapped[0], ImagePart)
    assert remapped[0].attachment_id == "attachment-2"
    assert remapped[0].detail == "high"
    assert source.attachment_id == "attachment-1"


def test_attachment_helpers_are_not_image_specific() -> None:
    source = DocumentPartForTest(
        mime_type="application/pdf",
        attachment_id="document-1",
        label="Requirements",
    )

    remapped = remap_attachment_ids([source], {"document-1": "document-2"})

    assert is_attachment_part(source)
    assert remapped is not None
    assert isinstance(remapped[0], DocumentPartForTest)
    assert remapped[0].attachment_id == "document-2"
    assert remapped[0].label == "Requirements"
