from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.execution import AGENT_BACKEND_PEER_AGENT, TenantExecutionContext
from app.models import MessageRole, TenantContext
from app.store import ThreadStore

_FEATURE_MCP = "mcp"
_FEATURE_PEER_AGENTS = "peer_agents"
_LIMIT_MAX_THREADS = "max_threads"
_LIMIT_MAX_MESSAGES_PER_THREAD = "max_messages_per_thread"
_LIMIT_MAX_MESSAGES = "max_messages"
_LIMIT_MAX_THREAD_RUNS = "max_thread_runs"


def enforce_thread_creation_limit(
    *,
    context: TenantContext,
    store: ThreadStore,
) -> None:
    max_threads = _optional_non_negative_int_limit(context, _LIMIT_MAX_THREADS)
    if max_threads is None:
        return
    current = store.count_threads(context.tenant_id)
    if current >= max_threads:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tenant limit '{_LIMIT_MAX_THREADS}' exceeded: "
                f"{current}/{max_threads} threads already exist"
            ),
        )


def enforce_message_creation_limit(
    *,
    context: TenantContext,
    store: ThreadStore,
    thread_id: str,
) -> None:
    max_messages = _optional_non_negative_int_limit(
        context,
        _LIMIT_MAX_MESSAGES_PER_THREAD,
        fallback_key=_LIMIT_MAX_MESSAGES,
    )
    if max_messages is None:
        return
    current = store.count_messages(context.tenant_id, thread_id)
    if current >= max_messages:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tenant limit '{_LIMIT_MAX_MESSAGES_PER_THREAD}' exceeded: "
                f"{current}/{max_messages} messages already exist in thread"
            ),
        )


def enforce_thread_run_limit(
    *,
    context: TenantContext,
    store: ThreadStore,
    thread_id: str,
) -> None:
    max_runs = _optional_non_negative_int_limit(context, _LIMIT_MAX_THREAD_RUNS)
    if max_runs is None:
        return
    current = sum(
        1
        for message in store.list_messages(context.tenant_id, thread_id)
        if message.role == MessageRole.ASSISTANT
    )
    if current >= max_runs:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tenant limit '{_LIMIT_MAX_THREAD_RUNS}' exceeded: "
                f"{current}/{max_runs} assistant runs already exist in thread"
            ),
        )


def enforce_execution_entitlements(
    *,
    context: TenantContext,
    execution: TenantExecutionContext,
) -> None:
    if execution.config.agent_backend.type == AGENT_BACKEND_PEER_AGENT:
        _require_feature_enabled(context, _FEATURE_PEER_AGENTS)
    if execution.config.tools.mcp_servers:
        _require_feature_enabled(context, _FEATURE_MCP)


def _require_feature_enabled(context: TenantContext, feature: str) -> None:
    enabled = context.features.get(feature)
    if enabled is False:
        raise HTTPException(
            status_code=403,
            detail=f"Tenant feature '{feature}' is disabled",
        )


def _optional_non_negative_int_limit(
    context: TenantContext,
    key: str,
    *,
    fallback_key: str | None = None,
) -> int | None:
    value = context.limits.get(key)
    effective_key = key
    if value is None and fallback_key is not None:
        value = context.limits.get(fallback_key)
        effective_key = fallback_key
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            status_code=500,
            detail=f"Tenant limit '{effective_key}' must be a non-negative integer",
        )
    if value < 0:
        raise HTTPException(
            status_code=500,
            detail=f"Tenant limit '{effective_key}' must be a non-negative integer",
        )
    return value


def tenant_context_from_request_state(state: Any) -> TenantContext:
    context = getattr(state, "tenant_context", None)
    if not isinstance(context, TenantContext):
        raise HTTPException(status_code=500, detail="Tenant context is unavailable")
    return context
