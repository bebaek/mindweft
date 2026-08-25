from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeGuard, TypeVar

from pydantic import BaseModel

from app.models import AttachmentPartBase, Message

PartT = TypeVar("PartT", bound=BaseModel)


def is_attachment_part(part: object) -> TypeGuard[AttachmentPartBase]:
    return isinstance(part, AttachmentPartBase)


def message_attachment_parts(message: Message) -> list[AttachmentPartBase]:
    return [part for part in (message.parts or []) if is_attachment_part(part)]


def attachment_ids(messages: Iterable[Message]) -> list[str]:
    return [
        part.attachment_id
        for message in messages
        for part in message_attachment_parts(message)
        if part.attachment_id is not None
    ]


def remap_attachment_ids(
    parts: list[PartT] | None,
    attachment_id_map: Mapping[str, str],
) -> list[PartT] | None:
    if parts is None:
        return None
    remapped: list[PartT] = []
    for part in parts:
        if is_attachment_part(part) and part.attachment_id is not None:
            remapped.append(
                part.model_copy(
                    deep=True,
                    update={
                        "attachment_id": attachment_id_map.get(
                            part.attachment_id,
                            part.attachment_id,
                        )
                    },
                )
            )
        else:
            remapped.append(part.model_copy(deep=True))
    return remapped
