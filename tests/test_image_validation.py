import pytest

from app.image_validation import ImageDimensionError, enforce_image_dimensions, image_dimensions


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _jpeg(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xe0\x00\x04ab"
        + b"\xff\xc0\x00\x07\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
    )


def _webp(chunk_type: bytes, payload: bytes) -> bytes:
    chunk = chunk_type + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk


def test_reads_png_gif_and_jpeg_dimensions() -> None:
    assert image_dimensions(_png(640, 480), "image/png") == (640, 480)
    assert image_dimensions(b"GIF89a" + b"\x40\x01\xf0\x00", "image/gif") == (320, 240)
    assert image_dimensions(_jpeg(1920, 1080), "image/jpeg") == (1920, 1080)


def test_reads_webp_variant_dimensions() -> None:
    vp8x_payload = b"\x00\x00\x00\x00" + (639).to_bytes(3, "little") + (479).to_bytes(3, "little")
    assert image_dimensions(_webp(b"VP8X", vp8x_payload), "image/webp") == (640, 480)

    vp8_payload = (
        b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + (320).to_bytes(2, "little")
        + (240).to_bytes(2, "little")
    )
    assert image_dimensions(_webp(b"VP8 ", vp8_payload), "image/webp") == (320, 240)

    width, height = 100, 50
    bits = (width - 1) | ((height - 1) << 14)
    vp8l_payload = b"\x2f" + bits.to_bytes(4, "little")
    assert image_dimensions(_webp(b"VP8L", vp8l_payload), "image/webp") == (width, height)


def test_enforces_pixel_and_dimension_limits() -> None:
    assert enforce_image_dimensions(
        _png(100, 50), "image/png", max_pixels=5_000, max_dimension=100
    ) == (100, 50)

    with pytest.raises(ImageDimensionError, match="pixel count"):
        enforce_image_dimensions(_png(100, 51), "image/png", max_pixels=5_000, max_dimension=100)
    with pytest.raises(ImageDimensionError, match="dimension"):
        enforce_image_dimensions(_png(101, 1), "image/png", max_pixels=5_000, max_dimension=100)
    with pytest.raises(ImageDimensionError, match="could not be determined"):
        enforce_image_dimensions(
            b"\x89PNG\r\n\x1a\n", "image/png", max_pixels=5_000, max_dimension=100
        )


def test_leaves_explicitly_configured_unknown_formats_to_provider_validation() -> None:
    assert (
        enforce_image_dimensions(b"custom", "image/example", max_pixels=1, max_dimension=1) is None
    )
