from __future__ import annotations

_DIMENSION_VALIDATED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_JPEG_START_OF_FRAME_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


class ImageDimensionError(ValueError):
    pass


def enforce_image_dimensions(
    data: bytes,
    mime_type: str,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[int, int] | None:
    """Validate dimensions for image formats whose headers Minigent understands.

    Returns ``None`` for explicitly configured formats without a dimension parser.
    """
    if mime_type not in _DIMENSION_VALIDATED_MIME_TYPES:
        return None
    dimensions = image_dimensions(data, mime_type)
    if dimensions is None:
        raise ImageDimensionError("image dimensions could not be determined")
    width, height = dimensions
    if width < 1 or height < 1:
        raise ImageDimensionError("image dimensions must be positive")
    if width > max_dimension or height > max_dimension:
        raise ImageDimensionError(
            f"image dimension exceeds maximum allowed length ({max_dimension}px)"
        )
    if width * height > max_pixels:
        raise ImageDimensionError(f"image exceeds maximum allowed pixel count ({max_pixels})")
    return dimensions


def image_dimensions(data: bytes, mime_type: str) -> tuple[int, int] | None:
    if mime_type == "image/png":
        return _png_dimensions(data)
    if mime_type == "image/jpeg":
        return _jpeg_dimensions(data)
    if mime_type == "image/gif":
        return _gif_dimensions(data)
    if mime_type == "image/webp":
        return _webp_dimensions(data)
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if (
        len(data) < 29
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[8:12] != (13).to_bytes(4, "big")
        or data[12:16] != b"IHDR"
    ):
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset < len(data):
        while offset < len(data) and data[offset] != 0xFF:
            offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            return None
        if marker in _JPEG_START_OF_FRAME_MARKERS:
            if segment_length < 7:
                return None
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height
        if marker == 0xDA:
            return None
        offset += segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    container_end = int.from_bytes(data[4:8], "little") + 8
    if container_end < 20 or container_end > len(data):
        return None
    chunk_type = data[12:16]
    chunk_size = int.from_bytes(data[16:20], "little")
    if 20 + chunk_size > container_end:
        return None
    if chunk_type == b"VP8X":
        if chunk_size < 10:
            return None
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk_type == b"VP8 ":
        if chunk_size < 10 or data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk_type == b"VP8L":
        if chunk_size < 5 or data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None
