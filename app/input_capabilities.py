from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.execution import (
    AGENT_BACKEND_NATIVE,
    TenantExecutionContext,
    get_llm_config,
)

ImageInputUnavailableReason = Literal[
    "disabled",
    "backend_unsupported",
    "profile_unsupported",
]


@dataclass(frozen=True)
class ImageInputAvailability:
    allowed: bool
    reason: ImageInputUnavailableReason | None = None
    capability_declared: bool = False


def image_input_availability(
    execution: TenantExecutionContext,
    llm_profile: str | None,
    *,
    globally_enabled: bool,
) -> ImageInputAvailability:
    """Return Mindweft's effective image-input policy for an execution profile.

    An omitted ``input_modalities`` declaration remains permissive for backward
    compatibility. The provider can still reject a model that does not actually
    accept images.
    """
    llm_config = get_llm_config(execution, llm_profile)
    input_modalities = llm_config.input_modalities
    declared = input_modalities is not None
    if not globally_enabled:
        return ImageInputAvailability(
            allowed=False,
            reason="disabled",
            capability_declared=declared,
        )
    if execution.config.agent_backend.type != AGENT_BACKEND_NATIVE:
        return ImageInputAvailability(
            allowed=False,
            reason="backend_unsupported",
            capability_declared=declared,
        )
    if input_modalities is not None and "image" not in input_modalities:
        return ImageInputAvailability(
            allowed=False,
            reason="profile_unsupported",
            capability_declared=True,
        )
    return ImageInputAvailability(allowed=True, capability_declared=declared)


def image_input_unavailable_detail(reason: ImageInputUnavailableReason) -> str:
    if reason == "disabled":
        return "image input is disabled"
    if reason == "backend_unsupported":
        return "selected agent backend does not support image input"
    return "selected LLM profile does not support image input"
