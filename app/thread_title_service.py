from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException

from app.execution import TenantExecutionContext, get_llm_adapter
from app.models import Message, MessageRole
from app.store import ThreadStore

logger = logging.getLogger(__name__)

_TITLE_SYSTEM_PROMPT = """Create a concise semantic title for this conversation.

Requirements:
- 4 to 8 words
- Describe the user's concrete objective or topic
- Prefer the affected feature, location, file, error, or task
- Do not describe the conversation itself
- Do not use quotation marks
- Do not end with punctuation
- Return only the title
- If there is not yet a concrete objective or topic, return INSUFFICIENT_CONTEXT
"""
_TITLE_SENTINEL = "INSUFFICIENT_CONTEXT"
_MAX_TRANSCRIPT_MESSAGES = 8
_MAX_TRANSCRIPT_CHARS = 4000
_MAX_TITLE_CHARS = 80
_AUTOMATIC_TITLE_PROVIDERS = {
    "anthropic",
    "azure-openai",
    "generic-oauth",
    "google",
    "openai",
    "openai-compatible",
    "openrouter",
}


@dataclass(frozen=True)
class SemanticTitleResult:
    status: Literal["updated", "skipped", "failed"]
    title: str | None = None
    reason: str | None = None


def automatic_semantic_titles_supported(
    execution: TenantExecutionContext,
    profile_name: str | None,
) -> bool:
    adapter = get_llm_adapter(execution, profile_name)
    provider = adapter.describe().get("provider")
    return provider in _AUTOMATIC_TITLE_PROVIDERS and getattr(adapter, "_transport", None) is None


async def generate_semantic_thread_title(
    *,
    store: ThreadStore,
    execution: TenantExecutionContext,
    tenant_id: str,
    thread_id: str,
) -> SemanticTitleResult:
    """Generate and atomically persist one semantic title for a thread."""
    try:
        thread = store.get_thread(tenant_id, thread_id)
        if thread.title_source == "manual":
            return SemanticTitleResult(status="skipped", reason="manual_title")
        if thread.title_source == "semantic":
            return SemanticTitleResult(
                status="skipped", title=thread.title, reason="already_semantic"
            )
        adapter = get_llm_adapter(execution, thread.llm_profile)
        transcript = _title_transcript(store.list_messages(tenant_id, thread_id))
        if not transcript:
            return SemanticTitleResult(status="skipped", reason="empty_thread")
        prompt_messages = [
            Message(thread_id=thread_id, role=MessageRole.SYSTEM, content=_TITLE_SYSTEM_PROMPT),
            Message(thread_id=thread_id, role=MessageRole.USER, content=transcript),
        ]
        response = await asyncio.wait_for(adapter.generate(prompt_messages, []), timeout=20.0)
        title = normalize_semantic_title(response.content)
        if title is None:
            reason = (
                "insufficient_context"
                if response.content
                and response.content.strip().casefold() == _TITLE_SENTINEL.casefold()
                else "invalid_response"
            )
            status: Literal["skipped", "failed"] = (
                "skipped" if reason == "insufficient_context" else "failed"
            )
            return SemanticTitleResult(status=status, reason=reason)
        updated = store.set_semantic_thread_title(tenant_id, thread_id, title=title)
        if updated.title_source == "manual":
            return SemanticTitleResult(status="skipped", title=updated.title, reason="manual_title")
        if updated.title_source == "semantic" and updated.title != title:
            return SemanticTitleResult(
                status="skipped", title=updated.title, reason="already_semantic"
            )
        return SemanticTitleResult(status="updated", title=title)
    except TimeoutError:
        logger.warning(
            "Thread title generation timed out tenant_id=%s thread_id=%s",
            tenant_id,
            thread_id,
        )
        return SemanticTitleResult(status="failed", reason="timeout")
    except HTTPException:
        logger.warning(
            "Thread title generation failed tenant_id=%s thread_id=%s",
            tenant_id,
            thread_id,
            exc_info=True,
        )
        return SemanticTitleResult(status="failed", reason="provider_error")
    except Exception:  # pragma: no cover - defensive background-task boundary
        logger.exception(
            "Unexpected thread title generation failure tenant_id=%s thread_id=%s",
            tenant_id,
            thread_id,
        )
        return SemanticTitleResult(status="failed", reason="unexpected_error")


def normalize_semantic_title(content: str | None) -> str | None:
    if not content:
        return None
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    title = re.sub(r"^title\s*:\s*", "", first_line, flags=re.IGNORECASE)
    title = " ".join(title.strip(" `\"'“”‘’").split()).rstrip(".!?:;,- ")
    if not title or title.casefold() == _TITLE_SENTINEL.casefold():
        return None
    if len(title) > _MAX_TITLE_CHARS:
        return None
    word_count = len(title.split())
    if word_count < 2 or word_count > 12:
        return None
    return title


def _title_transcript(messages: list[Message]) -> str:
    lines: list[str] = []
    remaining = _MAX_TRANSCRIPT_CHARS
    for message in messages:
        if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            continue
        if message.tool_name is not None or not message.content.strip():
            continue
        content = " ".join(message.content.split())
        if len(content) > remaining:
            content = content[:remaining].rstrip()
        if not content:
            break
        lines.append(f"{message.role.value}: {content}")
        remaining -= len(content)
        if len(lines) >= _MAX_TRANSCRIPT_MESSAGES or remaining <= 0:
            break
    return "\n".join(lines)
