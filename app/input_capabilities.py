from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.execution import AGENT_BACKEND_NATIVE, TenantExecutionContext, get_llm_config

InputUnavailableReason = Literal["disabled", "backend_unsupported", "profile_unsupported"]
ImageInputUnavailableReason = InputUnavailableReason


@dataclass(frozen=True)
class InputAvailability:
    allowed: bool
    reason: InputUnavailableReason | None = None
    capability_declared: bool = False


ImageInputAvailability = InputAvailability
DocumentInputAvailability = InputAvailability


def _input_availability(
    execution: TenantExecutionContext,
    llm_profile: str | None,
    *,
    modality: str,
    globally_enabled: bool,
    require_declaration: bool,
) -> InputAvailability:
    llm_config = get_llm_config(execution, llm_profile)
    input_modalities = llm_config.input_modalities
    declared = input_modalities is not None
    if not globally_enabled:
        return InputAvailability(False, "disabled", declared)
    if execution.config.agent_backend.type != AGENT_BACKEND_NATIVE:
        return InputAvailability(False, "backend_unsupported", declared)
    if modality == "document" and llm_config.provider not in {
        "mock",
        "anthropic",
        "google",
        "google-generative-ai",
        "gemini",
        "generic-oauth",
    }:
        return InputAvailability(False, "profile_unsupported", declared)
    if (require_declaration and input_modalities is None) or (
        input_modalities is not None and modality not in input_modalities
    ):
        return InputAvailability(False, "profile_unsupported", declared)
    return InputAvailability(True, capability_declared=declared)


def image_input_availability(
    execution: TenantExecutionContext,
    llm_profile: str | None,
    *,
    globally_enabled: bool,
) -> ImageInputAvailability:
    """Resolve image policy, retaining permissive omitted metadata for compatibility."""
    return _input_availability(
        execution,
        llm_profile,
        modality="image",
        globally_enabled=globally_enabled,
        require_declaration=False,
    )


def document_input_availability(
    execution: TenantExecutionContext,
    llm_profile: str | None,
    *,
    globally_enabled: bool,
) -> DocumentInputAvailability:
    """Resolve PDF policy; document support always requires an explicit declaration."""
    return _input_availability(
        execution,
        llm_profile,
        modality="document",
        globally_enabled=globally_enabled,
        require_declaration=True,
    )


def input_unavailable_detail(modality: str, reason: InputUnavailableReason) -> str:
    if reason == "disabled":
        return f"{modality} input is disabled"
    if reason == "backend_unsupported":
        return f"selected agent backend does not support {modality} input"
    return f"selected LLM profile does not support {modality} input"


def image_input_unavailable_detail(reason: ImageInputUnavailableReason) -> str:
    return input_unavailable_detail("image", reason)


def document_input_unavailable_detail(reason: InputUnavailableReason) -> str:
    return input_unavailable_detail("document", reason)
