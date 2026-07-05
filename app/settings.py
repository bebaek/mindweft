from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.admin_api import AdminStoreSettings
from app.runtime import RuntimeSettings
from app.store import ThreadStoreSettings

IMAGE_INPUT_ENABLED_ENV = "MINIGENT_IMAGE_INPUT_ENABLED"
IMAGE_INPUT_MAX_BYTES_ENV = "MINIGENT_IMAGE_INPUT_MAX_BYTES"
IMAGE_INPUT_ALLOWED_MIME_TYPES_ENV = "MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES"
DEFAULT_IMAGE_INPUT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


@dataclass(frozen=True)
class ImageInputSettings:
    enabled: bool = False
    max_bytes: int = DEFAULT_IMAGE_INPUT_MAX_BYTES
    allowed_mime_types: frozenset[str] = DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ImageInputSettings:
        lookup = os.environ if env is None else env
        return cls(
            enabled=_parse_image_input_enabled(lookup),
            max_bytes=_parse_image_input_max_bytes(lookup),
            allowed_mime_types=_parse_image_input_allowed_mime_types(lookup),
        )


@dataclass(frozen=True)
class MinigentSettings:
    admin_store: AdminStoreSettings
    image_input: ImageInputSettings
    runtime: RuntimeSettings
    thread_store: ThreadStoreSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MinigentSettings:
        lookup = os.environ if env is None else env
        return cls(
            admin_store=AdminStoreSettings.from_env(lookup),
            image_input=ImageInputSettings.from_env(lookup),
            runtime=RuntimeSettings.from_env(lookup),
            thread_store=ThreadStoreSettings.from_env(lookup),
        )


def load_settings(env: Mapping[str, str] | None = None) -> MinigentSettings:
    return MinigentSettings.from_env(env)


def image_input_settings_from_env() -> ImageInputSettings:
    return ImageInputSettings.from_env()


def _image_input_public_dict(settings: ImageInputSettings) -> dict[str, object]:
    return {
        "enabled": settings.enabled,
        "max_bytes": settings.max_bytes,
        "allowed_mime_types": sorted(settings.allowed_mime_types),
    }


def _image_input_export_public_dict(settings: ImageInputSettings) -> dict[str, object]:
    exported: dict[str, object] = {}
    if settings.enabled:
        exported["enabled"] = True
    if settings.max_bytes != DEFAULT_IMAGE_INPUT_MAX_BYTES:
        exported["max_bytes"] = settings.max_bytes
    if settings.allowed_mime_types != DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES:
        exported["allowed_mime_types"] = sorted(settings.allowed_mime_types)
    return exported


def _parse_image_input_enabled(env: Mapping[str, str]) -> bool:
    value = env.get(IMAGE_INPUT_ENABLED_ENV)
    if value is None:
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{IMAGE_INPUT_ENABLED_ENV} must be a boolean")


def _parse_image_input_allowed_mime_types(env: Mapping[str, str]) -> frozenset[str]:
    configured = env.get(IMAGE_INPUT_ALLOWED_MIME_TYPES_ENV, "").strip()
    if not configured:
        return DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES
    return frozenset(item.strip().lower() for item in configured.split(",") if item.strip())


def _parse_image_input_max_bytes(env: Mapping[str, str]) -> int:
    configured = env.get(IMAGE_INPUT_MAX_BYTES_ENV, "").strip()
    if not configured:
        return DEFAULT_IMAGE_INPUT_MAX_BYTES
    try:
        value = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{IMAGE_INPUT_MAX_BYTES_ENV} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{IMAGE_INPUT_MAX_BYTES_ENV} must be a positive integer")
    return value
