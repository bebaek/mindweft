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
from app.rate_limits import RateLimitSettings
from app.runtime import RuntimeSettings
from app.store import ThreadStoreSettings
from mindweft_config.unified_config import normalize_mindweft_env

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
DOCUMENT_INPUT_ENABLED_ENV = "MINIGENT_DOCUMENT_INPUT_ENABLED"
DOCUMENT_INPUT_MAX_BYTES_ENV = "MINIGENT_DOCUMENT_INPUT_MAX_BYTES"
DOCUMENT_INPUT_MAX_DOCUMENTS_ENV = "MINIGENT_DOCUMENT_INPUT_MAX_DOCUMENTS"
DOCUMENT_INPUT_MAX_TOTAL_BYTES_ENV = "MINIGENT_DOCUMENT_INPUT_MAX_TOTAL_BYTES"
DOCUMENT_INPUT_ALLOWED_MIME_TYPES_ENV = "MINIGENT_DOCUMENT_INPUT_ALLOWED_MIME_TYPES"
DEFAULT_DOCUMENT_INPUT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_DOCUMENT_INPUT_MAX_DOCUMENTS = 4
DEFAULT_DOCUMENT_INPUT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
DEFAULT_DOCUMENT_INPUT_ALLOWED_MIME_TYPES = frozenset({"application/pdf"})


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
        lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
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
class DocumentInputSettings:
    enabled: bool = False
    max_bytes: int = DEFAULT_DOCUMENT_INPUT_MAX_BYTES
    max_documents: int = DEFAULT_DOCUMENT_INPUT_MAX_DOCUMENTS
    max_total_bytes: int = DEFAULT_DOCUMENT_INPUT_MAX_TOTAL_BYTES
    allowed_mime_types: frozenset[str] = DEFAULT_DOCUMENT_INPUT_ALLOWED_MIME_TYPES

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DocumentInputSettings:
        lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
        return cls(
            enabled=_parse_boolean(lookup, DOCUMENT_INPUT_ENABLED_ENV),
            max_bytes=_parse_positive_int(
                lookup, DOCUMENT_INPUT_MAX_BYTES_ENV, DEFAULT_DOCUMENT_INPUT_MAX_BYTES
            ),
            max_documents=_parse_positive_int(
                lookup, DOCUMENT_INPUT_MAX_DOCUMENTS_ENV, DEFAULT_DOCUMENT_INPUT_MAX_DOCUMENTS
            ),
            max_total_bytes=_parse_positive_int(
                lookup, DOCUMENT_INPUT_MAX_TOTAL_BYTES_ENV, DEFAULT_DOCUMENT_INPUT_MAX_TOTAL_BYTES
            ),
            allowed_mime_types=_parse_allowed_mime_types(
                lookup,
                DOCUMENT_INPUT_ALLOWED_MIME_TYPES_ENV,
                DEFAULT_DOCUMENT_INPUT_ALLOWED_MIME_TYPES,
            ),
        )


@dataclass(frozen=True)
class MindweftSettings:
    admin_store: AdminStoreSettings
    agent_backend: TenantAgentBackendConfig
    attachment_store: AttachmentStoreSettings
    auth: AuthSettings
    document_input: DocumentInputSettings
    image_input: ImageInputSettings
    llm: LLMSettings
    logging: LoggingSettings
    mcp: MCPSettings
    peer_agents: PeerAgentSettings
    quality: TenantQualityConfig
    rate_limits: RateLimitSettings
    runtime: RuntimeSettings
    tenant_execution: TenantExecutionSettings
    thread_store: ThreadStoreSettings
    tracing: TracingSettings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MindweftSettings:
        lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
        return cls(
            admin_store=AdminStoreSettings.from_env(lookup),
            agent_backend=TenantAgentBackendConfig.from_env(lookup),
            attachment_store=AttachmentStoreSettings.from_env(lookup),
            auth=AuthSettings.from_env(lookup),
            document_input=DocumentInputSettings.from_env(lookup),
            image_input=ImageInputSettings.from_env(lookup),
            llm=LLMSettings.from_env(lookup),
            logging=LoggingSettings.from_env(lookup),
            mcp=MCPSettings.from_env(lookup),
            peer_agents=PeerAgentSettings.from_env(lookup),
            quality=TenantQualityConfig.from_env(lookup),
            rate_limits=RateLimitSettings.from_env(lookup),
            runtime=RuntimeSettings.from_env(lookup),
            tenant_execution=TenantExecutionSettings.from_env(lookup),
            thread_store=ThreadStoreSettings.from_env(lookup),
            tracing=TracingSettings.from_env(lookup),
        )


# Backward-compatible public alias.
MinigentSettings = MindweftSettings


def load_settings(env: Mapping[str, str] | None = None) -> MindweftSettings:
    return MindweftSettings.from_env(env)


def document_input_settings_from_env() -> DocumentInputSettings:
    return DocumentInputSettings.from_env()


def image_input_settings_from_env() -> ImageInputSettings:
    return ImageInputSettings.from_env()


def _document_input_public_dict(settings: DocumentInputSettings) -> dict[str, object]:
    return {
        "enabled": settings.enabled,
        "max_bytes": settings.max_bytes,
        "max_documents": settings.max_documents,
        "max_total_bytes": settings.max_total_bytes,
        "allowed_mime_types": sorted(settings.allowed_mime_types),
    }


def _document_input_export_public_dict(settings: DocumentInputSettings) -> dict[str, object]:
    exported: dict[str, object] = {}
    if settings.enabled:
        exported["enabled"] = True
    if settings.max_bytes != DEFAULT_DOCUMENT_INPUT_MAX_BYTES:
        exported["max_bytes"] = settings.max_bytes
    if settings.max_documents != DEFAULT_DOCUMENT_INPUT_MAX_DOCUMENTS:
        exported["max_documents"] = settings.max_documents
    if settings.max_total_bytes != DEFAULT_DOCUMENT_INPUT_MAX_TOTAL_BYTES:
        exported["max_total_bytes"] = settings.max_total_bytes
    if settings.allowed_mime_types != DEFAULT_DOCUMENT_INPUT_ALLOWED_MIME_TYPES:
        exported["allowed_mime_types"] = sorted(settings.allowed_mime_types)
    return exported


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


def _canonical_env_name(name: str) -> str:
    return name.replace("MINIGENT_", "MINDWEFT_", 1)


def _parse_image_input_enabled(env: Mapping[str, str]) -> bool:
    return _parse_boolean(env, IMAGE_INPUT_ENABLED_ENV)


def _parse_boolean(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    if value is None:
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{_canonical_env_name(name)} must be a boolean")


def _parse_image_input_allowed_mime_types(env: Mapping[str, str]) -> frozenset[str]:
    return _parse_allowed_mime_types(
        env, IMAGE_INPUT_ALLOWED_MIME_TYPES_ENV, DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES
    )


def _parse_allowed_mime_types(
    env: Mapping[str, str], name: str, default: frozenset[str]
) -> frozenset[str]:
    configured = env.get(name, "").strip()
    if not configured:
        return default
    return frozenset(item.strip().lower() for item in configured.split(",") if item.strip())


def _parse_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    configured = env.get(name, "").strip()
    if not configured:
        return default
    try:
        value = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{_canonical_env_name(name)} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{_canonical_env_name(name)} must be a positive integer")
    return value
