from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.admin_api import AdminStoreSettings
from app.attachments import AttachmentStoreSettings
from app.auth import AuthSettings
from app.execution import (
    TenantAgentBackendConfig,
    TenantExecutionSettings,
    TenantQualityConfig,
)
from app.llm import LLMSettings
from app.mcp import MCPSettings
from app.observability import LoggingSettings, TracingSettings
from app.peer_agents import PeerAgentSettings
from app.runtime import RuntimeSettings
from app.store import ThreadStoreSettings

IMAGE_INPUT_ENABLED_ENV = "MINIGENT_IMAGE_INPUT_ENABLED"
IMAGE_INPUT_MAX_BYTES_ENV = "MINIGENT_IMAGE_INPUT_MAX_BYTES"
IMAGE_INPUT_MAX_IMAGES_ENV = "MINIGENT_IMAGE_INPUT_MAX_IMAGES"
IMAGE_INPUT_MAX_TOTAL_BYTES_ENV = "MINIGENT_IMAGE_INPUT_MAX_TOTAL_BYTES"
IMAGE_INPUT_MAX_PIXELS_ENV = "MINIGENT_IMAGE_INPUT_MAX_PIXELS"
IMAGE_INPUT_MAX_DIMENSION_ENV = "MINIGENT_IMAGE_INPUT_MAX_DIMENSION"
IMAGE_INPUT_ALLOWED_MIME_TYPES_ENV = "MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES"
DEFAULT_IMAGE_INPUT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_IMAGE_INPUT_MAX_IMAGES = 8
DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
DEFAULT_IMAGE_INPUT_MAX_PIXELS = 64_000_000
DEFAULT_IMAGE_INPUT_MAX_DIMENSION = 16_384
DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


@dataclass(frozen=True)
class ImageInputSettings:
    enabled: bool = False
    max_bytes: int = DEFAULT_IMAGE_INPUT_MAX_BYTES
    max_images: int = DEFAULT_IMAGE_INPUT_MAX_IMAGES
    max_total_bytes: int = DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES
    max_pixels: int = DEFAULT_IMAGE_INPUT_MAX_PIXELS
    max_dimension: int = DEFAULT_IMAGE_INPUT_MAX_DIMENSION
    allowed_mime_types: frozenset[str] = DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ImageInputSettings:
        lookup = os.environ if env is None else env
        return cls(
            enabled=_parse_image_input_enabled(lookup),
            max_bytes=_parse_positive_int(
                lookup, IMAGE_INPUT_MAX_BYTES_ENV, DEFAULT_IMAGE_INPUT_MAX_BYTES
            ),
            max_images=_parse_positive_int(
                lookup, IMAGE_INPUT_MAX_IMAGES_ENV, DEFAULT_IMAGE_INPUT_MAX_IMAGES
            ),
            max_total_bytes=_parse_positive_int(
                lookup,
                IMAGE_INPUT_MAX_TOTAL_BYTES_ENV,
                DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES,
            ),
            max_pixels=_parse_positive_int(
                lookup, IMAGE_INPUT_MAX_PIXELS_ENV, DEFAULT_IMAGE_INPUT_MAX_PIXELS
            ),
            max_dimension=_parse_positive_int(
                lookup, IMAGE_INPUT_MAX_DIMENSION_ENV, DEFAULT_IMAGE_INPUT_MAX_DIMENSION
            ),
            allowed_mime_types=_parse_image_input_allowed_mime_types(lookup),
        )


@dataclass(frozen=True)
class MinigentSettings:
    admin_store: AdminStoreSettings
    agent_backend: TenantAgentBackendConfig
    attachment_store: AttachmentStoreSettings
    auth: AuthSettings
    image_input: ImageInputSettings
    llm: LLMSettings
    logging: LoggingSettings
    mcp: MCPSettings
    peer_agents: PeerAgentSettings
    quality: TenantQualityConfig
    runtime: RuntimeSettings
    tenant_execution: TenantExecutionSettings
    thread_store: ThreadStoreSettings
    tracing: TracingSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MinigentSettings:
        lookup = os.environ if env is None else env
        return cls(
            admin_store=AdminStoreSettings.from_env(lookup),
            agent_backend=TenantAgentBackendConfig.from_env(lookup),
            attachment_store=AttachmentStoreSettings.from_env(lookup),
            auth=AuthSettings.from_env(lookup),
            image_input=ImageInputSettings.from_env(lookup),
            llm=LLMSettings.from_env(lookup),
            logging=LoggingSettings.from_env(lookup),
            mcp=MCPSettings.from_env(lookup),
            peer_agents=PeerAgentSettings.from_env(lookup),
            quality=TenantQualityConfig.from_env(lookup),
            runtime=RuntimeSettings.from_env(lookup),
            tenant_execution=TenantExecutionSettings.from_env(lookup),
            thread_store=ThreadStoreSettings.from_env(lookup),
            tracing=TracingSettings.from_env(lookup),
        )


def load_settings(env: Mapping[str, str] | None = None) -> MinigentSettings:
    return MinigentSettings.from_env(env)


def image_input_settings_from_env() -> ImageInputSettings:
    return ImageInputSettings.from_env()


def _image_input_public_dict(settings: ImageInputSettings) -> dict[str, object]:
    return {
        "enabled": settings.enabled,
        "max_bytes": settings.max_bytes,
        "max_images": settings.max_images,
        "max_total_bytes": settings.max_total_bytes,
        "max_pixels": settings.max_pixels,
        "max_dimension": settings.max_dimension,
        "allowed_mime_types": sorted(settings.allowed_mime_types),
    }


def _image_input_export_public_dict(settings: ImageInputSettings) -> dict[str, object]:
    exported: dict[str, object] = {}
    if settings.enabled:
        exported["enabled"] = True
    if settings.max_bytes != DEFAULT_IMAGE_INPUT_MAX_BYTES:
        exported["max_bytes"] = settings.max_bytes
    if settings.max_images != DEFAULT_IMAGE_INPUT_MAX_IMAGES:
        exported["max_images"] = settings.max_images
    if settings.max_total_bytes != DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES:
        exported["max_total_bytes"] = settings.max_total_bytes
    if settings.max_pixels != DEFAULT_IMAGE_INPUT_MAX_PIXELS:
        exported["max_pixels"] = settings.max_pixels
    if settings.max_dimension != DEFAULT_IMAGE_INPUT_MAX_DIMENSION:
        exported["max_dimension"] = settings.max_dimension
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


def _parse_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    configured = env.get(name, "").strip()
    if not configured:
        return default
    try:
        value = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value
