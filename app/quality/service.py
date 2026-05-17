from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.execution import TenantQualityConfig
from app.llm import LLMAdapter, MockLLMAdapter, OpenAICompatibleAdapter
from app.models import Message, MessageRole
from app.quality.prompts import REMOTE_CRITIQUE_SYSTEM_PROMPT, critique_user_prompt
from app.quality.sanitizer import Redaction, sanitize_for_remote


@dataclass(frozen=True)
class QualityEnhancementResult:
    used_remote: bool
    blocked: bool
    reason: str
    critique: str | None = None
    redactions: list[Redaction] | None = None


class QualityEnhancer:
    async def maybe_critique_draft(
        self,
        *,
        config: TenantQualityConfig,
        tenant_id: str,
        thread_id: str,
        local_draft: str,
        event_sink: Any = None,
    ) -> QualityEnhancementResult:
        _ = tenant_id
        if not config.enabled:
            return QualityEnhancementResult(
                False, False, "quality enhancement disabled", redactions=[]
            )
        await _emit(
            event_sink,
            {"type": "quality.considered", "mode": config.mode, "enabled": config.enabled},
        )
        if config.mode != "critique_draft":
            await _emit(event_sink, {"type": "quality.blocked", "reason": "unsupported mode"})
            return QualityEnhancementResult(
                False, True, f"unsupported quality mode '{config.mode}'", redactions=[]
            )

        sanitized = sanitize_for_remote(local_draft, max_chars=config.max_payload_chars)
        await _emit(
            event_sink,
            {
                "type": "quality.sanitized",
                "redaction_count": len(sanitized.redactions),
                "blocked": sanitized.blocked,
                "block_reason": sanitized.block_reason,
            },
        )
        if sanitized.blocked:
            await _emit(
                event_sink,
                {
                    "type": "quality.blocked",
                    "reason": sanitized.block_reason or "sanitizer blocked payload",
                },
            )
            return QualityEnhancementResult(
                False,
                True,
                sanitized.block_reason or "sanitizer blocked payload",
                redactions=sanitized.redactions,
            )

        try:
            adapter = _build_quality_adapter(config)
        except RuntimeError as exc:
            await _emit(event_sink, {"type": "quality.blocked", "reason": str(exc)})
            return QualityEnhancementResult(False, True, str(exc), redactions=sanitized.redactions)

        messages = [
            Message(
                thread_id=thread_id, role=MessageRole.SYSTEM, content=REMOTE_CRITIQUE_SYSTEM_PROMPT
            ),
            Message(
                thread_id=thread_id,
                role=MessageRole.USER,
                content=critique_user_prompt(sanitized.text),
            ),
        ]
        await _emit(
            event_sink,
            {
                "type": "quality.remote_request",
                "mode": config.mode,
                "provider": adapter.describe().get("provider"),
                "model": adapter.describe().get("model"),
                "redaction_count": len(sanitized.redactions),
            },
        )
        try:
            response = await adapter.generate(messages, [])
        except Exception as exc:  # pragma: no cover - defensive advisory boundary
            await _emit(event_sink, {"type": "quality.error", "detail": str(exc)})
            return QualityEnhancementResult(
                False, False, "remote quality request failed", redactions=sanitized.redactions
            )
        if response.content is None:
            await _emit(
                event_sink,
                {"type": "quality.error", "detail": "remote quality response had no content"},
            )
            return QualityEnhancementResult(
                False,
                False,
                "remote quality response had no content",
                redactions=sanitized.redactions,
            )
        await _emit(event_sink, {"type": "quality.remote_response", "has_critique": True})
        return QualityEnhancementResult(
            True,
            False,
            "remote critique received",
            critique=response.content,
            redactions=sanitized.redactions,
        )


def _build_quality_adapter(config: TenantQualityConfig) -> LLMAdapter:
    if config.provider == "mock":
        return MockLLMAdapter()
    if config.provider in {"openai", "openrouter", "openai-compatible"}:
        if not config.api_key:
            raise RuntimeError(f"Quality provider '{config.provider}' requires api_key")
        if not config.model:
            raise RuntimeError(f"Quality provider '{config.provider}' requires model")
        base_url = config.base_url or (
            "https://openrouter.ai/api/v1"
            if config.provider == "openrouter"
            else "https://api.openai.com/v1"
        )
        return OpenAICompatibleAdapter(
            base_url=base_url,
            api_key=config.api_key,
            model=config.model,
            extra_headers=config.extra_headers,
            timeout=config.timeout,
        )
    raise RuntimeError(f"Unsupported quality provider '{config.provider}'")


async def _emit(event_sink: Any, event: dict[str, object]) -> None:
    if event_sink is not None:
        await event_sink(event)
