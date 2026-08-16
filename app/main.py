from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.admin_api import build_admin_router
from app.admin_mcp import (
    AdminMCPAuthMiddleware,
    build_admin_chat_tool_registry,
    build_admin_mcp_server,
)
from app.admin_store import SQLiteTenantConfigStore
from app.agent_backends import AgentBackendRouter, NativeAgentBackend
from app.attachments import (
    AttachmentLimitExceeded,
    AttachmentMetadata,
    AttachmentStore,
    UploadAttachmentRequest,
    build_attachment_store,
)
from app.auth import validate_auth_settings
from app.entitlements import (
    enforce_execution_entitlements,
    enforce_message_creation_limit,
    enforce_thread_creation_limit,
    enforce_thread_run_limit,
    tenant_context_from_request_state,
)
from app.execution import (
    ADMIN_EXECUTION_CONFIG_KEY,
    TENANT_CONFIG_SOURCE_ENV_ONLY,
    TENANT_CONFIG_SOURCE_STORE,
    TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS,
    FixedTenantExecutionResolver,
    StoreBackedTenantExecutionResolver,
    TenantExecutionContext,
    TenantExecutionResolver,
    build_execution_resolver_from_env,
    get_llm_config,
    resolve_tenant_config_source,
)
from app.external_grants import build_external_grant_provider_registry_from_env
from app.health import database_readiness_checks
from app.image_validation import ImageDimensionError, enforce_image_dimensions
from app.llm import LLMAdapter, build_llm_adapter_from_env
from app.mcp_broker import (
    MCPBrokerSession,
    build_mcp_broker_session_store_from_env,
    handle_mcp_broker_request,
)
from app.mcp_manager import MCPServerManager
from app.models import (
    AddMessageRequest,
    CreateThreadRequest,
    CreateThreadResponse,
    ExecutionAgentOptionItem,
    ExecutionAgentOptionSection,
    ExecutionOptionItem,
    ExecutionOptionSection,
    ExecutionOptionsResponse,
    Message,
    MessageRole,
    Principal,
    PrivateValueConsentDecisionRequest,
    RunThreadResponse,
    TenantContext,
    TextPart,
    Thread,
    ThreadListItem,
    ThreadListResponse,
    ThreadStatus,
    ThreadTitleResponse,
    UpdateThreadTitleRequest,
)
from app.oauth import GenericOAuthProvider, build_oauth_flow_store_from_env
from app.observability import configure_logging, configure_tracing
from app.peer_agents import PeerAgentRegistry, build_peer_agent_registry
from app.quality import QualityEnhancer
from app.rate_limits import (
    RateLimiter,
    RunConcurrencyLease,
    build_rate_limiter,
)
from app.redaction import install_log_redaction
from app.runtime import (
    AgentRuntime,
    estimate_thread_context_usage,
    render_raw_thread_context,
)
from app.security_headers import SecurityHeadersMiddleware
from app.session_auth import build_session_auth_router, validate_session_auth_settings
from app.settings import (
    DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES,
    DEFAULT_IMAGE_INPUT_MAX_BYTES,
    ImageInputSettings,
    MinigentSettings,
    _image_input_export_public_dict,
    _image_input_public_dict,
    load_settings,
)
from app.store import (
    DEFAULT_RUN_LEASE_SECONDS,
    InMemoryThreadStore,
    SQLiteThreadStore,
    ThreadStore,
    ThreadStoreSettings,
)
from app.tenants import require_active_tenant_principal, require_tenant_context
from app.thread_titles import generate_thread_title, normalize_manual_thread_title
from app.tools import ToolRegistry, build_tool_registry_from_env
from app.user_deprovisioning import UserDeprovisioningProcessor
from app.user_execution import (
    UserExecutionResolutionError,
    effective_execution_catalog,
)
from app.user_execution_api import build_user_execution_router
from app.user_mcp import (
    UserMCPAuthMiddleware,
    build_user_mcp_server,
    build_user_mcp_tool_registry,
)
from minigent_config.environment import load_environment

__all__ = [
    "DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES",
    "DEFAULT_IMAGE_INPUT_MAX_BYTES",
    "ImageInputSettings",
    "create_app",
]

load_environment()
# Redact secrets in third-party logs like httpx request lines before any handler formats the record.
install_log_redaction()
configure_logging()

logger = logging.getLogger(__name__)
WEB_CLIENT_DIR = Path(__file__).resolve().parent / "static" / "web"
CONSOLE_CLIENT_DIR = Path(__file__).resolve().parent / "static" / "console"
STALE_RUN_RECOVERY_INTERVAL_SECONDS = 5.0
PEER_TASK_CANCELLATION_CLAIM_SECONDS = 30.0
PEER_TASK_CANCELLATION_BATCH_SIZE = 10
UPLOAD_RATE_LIMIT_CATEGORY = "attachment_upload"
RUN_RATE_LIMIT_CATEGORY = "thread_run"


def _peer_task_retry_delay(attempts: int) -> float:
    return min(300.0, float(2 ** min(max(attempts, 1), 8)))


async def _cleanup_pending_attachments_periodically(
    store: AttachmentStore,
    *,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        started_at = monotonic()
        try:
            result = await asyncio.to_thread(store.delete_expired_pending_with_stats)
        except Exception:  # pragma: no cover - defensive background boundary
            logger.exception(
                "attachment.pending_cleanup_failed trigger=scheduled duration_ms=%s",
                round((monotonic() - started_at) * 1000, 3),
            )
            continue
        if result.deleted_count:
            logger.info(
                "attachment.pending_cleanup_completed "
                "trigger=scheduled deleted_count=%s deleted_bytes=%s duration_ms=%s",
                result.deleted_count,
                result.deleted_bytes,
                round((monotonic() - started_at) * 1000, 3),
            )


def _cleanup_pending_attachments_before_upload(store: AttachmentStore) -> None:
    started_at = monotonic()
    result = store.delete_expired_pending_with_stats()
    if result.deleted_count:
        logger.info(
            "attachment.pending_cleanup_completed "
            "trigger=upload deleted_count=%s deleted_bytes=%s duration_ms=%s",
            result.deleted_count,
            result.deleted_bytes,
            round((monotonic() - started_at) * 1000, 3),
        )


async def _recover_stale_runs_periodically(
    store: ThreadStore,
    peer_agent_registry: PeerAgentRegistry | None = None,
    *,
    interval_seconds: float = STALE_RUN_RECOVERY_INTERVAL_SECONDS,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            recovered = await asyncio.to_thread(store.recover_stale_runs)
        except Exception:  # pragma: no cover - defensive background boundary
            logger.exception("thread_run.stale_recovery_failed")
            continue
        if recovered:
            logger.warning("thread_run.stale_recovered count=%s", recovered)
        if peer_agent_registry is None:
            continue
        cancellations = await asyncio.to_thread(
            store.claim_peer_task_cancellations,
            lease_seconds=PEER_TASK_CANCELLATION_CLAIM_SECONDS,
            limit=PEER_TASK_CANCELLATION_BATCH_SIZE,
        )
        for cancellation in cancellations:
            try:
                await peer_agent_registry.cancel_task_at(
                    cancellation.peer_name,
                    cancellation.peer_base_url,
                    cancellation.task_id,
                )
            except HTTPException:
                delay = _peer_task_retry_delay(cancellation.attempts)
                await asyncio.to_thread(
                    store.release_peer_task_cancellation,
                    cancellation.cancellation_id,
                    retry_delay_seconds=delay,
                )
                logger.warning(
                    "peer_task.orphan_cancel_retry peer=%s attempts=%s delay_seconds=%s",
                    cancellation.peer_name,
                    cancellation.attempts,
                    delay,
                )
                continue
            completed = await asyncio.to_thread(
                store.complete_peer_task_cancellation,
                cancellation.cancellation_id,
            )
            if completed:
                logger.warning(
                    "peer_task.orphan_canceled peer=%s attempts=%s",
                    cancellation.peer_name,
                    cancellation.attempts,
                )


def _enforce_request_rate_limit(
    request: Request,
    principal: Principal,
    category: str,
) -> None:
    settings = request.app.state.rate_limit_settings
    policy = settings.uploads if category == UPLOAD_RATE_LIMIT_CATEGORY else settings.runs
    decision = request.app.state.rate_limiter.consume(
        category,
        principal.tenant_id,
        principal.user_id,
        policy,
    )
    if decision.allowed:
        return
    logger.warning(
        "request.rate_limited category=%s tenant_id=%s scope=%s retry_after_seconds=%s",
        category,
        principal.tenant_id,
        decision.rejected_scope,
        decision.retry_after_seconds,
    )
    raise HTTPException(
        status_code=429,
        detail={
            "error": "rate_limit_exceeded",
            "category": category,
            "retry_after_seconds": decision.retry_after_seconds,
        },
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


async def _acquire_run_concurrency_slot(
    request: Request,
    principal: Principal,
) -> RunConcurrencyLease:
    policy = request.app.state.rate_limit_settings.concurrent_runs
    decision = await asyncio.to_thread(
        request.app.state.rate_limiter.acquire_run_slot,
        principal.tenant_id,
        principal.user_id,
        policy,
    )
    if decision.allowed and decision.lease is not None:
        return decision.lease
    logger.warning(
        "request.concurrency_limited tenant_id=%s scope=%s retry_after_seconds=%s",
        principal.tenant_id,
        decision.rejected_scope,
        decision.retry_after_seconds,
    )
    raise HTTPException(
        status_code=429,
        detail={
            "error": "concurrent_run_limit_exceeded",
            "category": "thread_run_concurrency",
            "retry_after_seconds": decision.retry_after_seconds,
        },
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


@asynccontextmanager
async def _maintain_run_concurrency_slot(
    app: FastAPI,
    lease: RunConcurrencyLease,
):
    policy = app.state.rate_limit_settings.concurrent_runs
    owner_task = asyncio.current_task()

    async def heartbeat() -> None:
        while lease.lease_id is not None:
            await asyncio.sleep(policy.heartbeat_seconds)
            renewed = await asyncio.to_thread(
                app.state.rate_limiter.renew_run_slot,
                lease,
                policy,
            )
            if renewed:
                continue
            logger.error(
                "thread_run.concurrency_lease_lost tenant_id=%s",
                lease.tenant_id,
            )
            if owner_task is not None:
                owner_task.cancel()
            return

    heartbeat_task = asyncio.create_task(heartbeat()) if lease.lease_id is not None else None
    try:
        yield
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(app.state.rate_limiter.release_run_slot, lease)


async def _run_thread_stream_with_concurrency_slot(
    request: Request,
    principal: Principal,
    thread_id: str,
    lease: RunConcurrencyLease,
) -> AsyncIterator[str]:
    async with _maintain_run_concurrency_slot(request.app, lease):
        async for event in _run_thread_ndjson_stream(request, principal, thread_id):
            yield event


def build_thread_store(settings: ThreadStoreSettings) -> ThreadStore:
    if settings.db_path is not None:
        return SQLiteThreadStore(settings.db_path)
    return InMemoryThreadStore()


def build_thread_store_from_env() -> ThreadStore:
    return build_thread_store(load_settings().thread_store)


def _thread_list_item(store: ThreadStore, tenant_id: str, thread: Thread) -> ThreadListItem:
    messages = store.list_messages(tenant_id, thread.thread_id)
    return ThreadListItem(
        thread_id=thread.thread_id,
        title=thread.title or _thread_title(messages),
        title_source=thread.title_source,
        title_updated_at=thread.title_updated_at,
        status=thread.status,
        skill_name=thread.skill_name,
        skill_names=thread.skill_names,
        capability_profile=thread.capability_profile,
        llm_profile=thread.llm_profile,
        message_count=len(messages),
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _thread_title(messages: list[Message]) -> str:
    for message in messages:
        if message.role == MessageRole.USER and message.content.strip():
            return generate_thread_title(message.content)
    for message in messages:
        if message.content.strip():
            return generate_thread_title(message.content)
    return "New conversation"


def _validate_and_normalize_message_request(
    request: AddMessageRequest,
    settings: ImageInputSettings,
    *,
    input_modalities: frozenset[str] | None = None,
    attachment_store: AttachmentStore | None = None,
    tenant_id: str | None = None,
    thread_id: str | None = None,
) -> AddMessageRequest:
    if not request.parts:
        if not request.content:
            raise HTTPException(status_code=400, detail="message content is required")
        return request
    image_parts = [part for part in request.parts if part.type == "image"]
    if image_parts and not settings.enabled:
        raise HTTPException(status_code=400, detail="image input is disabled")
    if image_parts and input_modalities is not None and "image" not in input_modalities:
        raise HTTPException(
            status_code=400, detail="selected LLM profile does not support image input"
        )
    if len(image_parts) > settings.max_images:
        raise HTTPException(
            status_code=400,
            detail=f"message exceeds maximum image count ({settings.max_images})",
        )
    total_image_bytes = 0
    for part in image_parts:
        mime_type = part.mime_type.lower()
        if mime_type not in settings.allowed_mime_types:
            raise HTTPException(
                status_code=400, detail=f"unsupported image MIME type: {part.mime_type}"
            )
        source_count = sum(bool(source) for source in (part.data, part.url, part.attachment_id))
        if source_count != 1:
            raise HTTPException(
                status_code=400,
                detail="image part must include exactly one of data, url, or attachment_id",
            )
        if part.attachment_id:
            if attachment_store is None or tenant_id is None or thread_id is None:
                raise HTTPException(status_code=400, detail="image attachment_id is not supported")
            record = attachment_store.get(tenant_id, thread_id, part.attachment_id)
            if record is None:
                raise HTTPException(status_code=400, detail="image attachment_id is invalid")
            if record.metadata.mime_type != mime_type:
                raise HTTPException(
                    status_code=400,
                    detail="image attachment MIME type does not match uploaded attachment",
                )
            _enforce_image_dimension_limits(record.data, mime_type, settings)
            total_image_bytes += record.metadata.size_bytes
        if part.url:
            parsed_url = urlsplit(part.url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise HTTPException(
                    status_code=400,
                    detail="image URL must be an absolute HTTP or HTTPS URL",
                )
        if part.data:
            try:
                decoded = base64.b64decode(part.data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=400, detail="image data must be base64") from exc
            if len(decoded) > settings.max_bytes:
                raise HTTPException(status_code=400, detail="image exceeds maximum allowed size")
            if not _image_bytes_match_mime_type(decoded, mime_type):
                raise HTTPException(
                    status_code=400,
                    detail=f"image data does not match declared MIME type: {part.mime_type}",
                )
            _enforce_image_dimension_limits(decoded, mime_type, settings)
            total_image_bytes += len(decoded)
        if total_image_bytes > settings.max_total_bytes:
            raise HTTPException(
                status_code=400,
                detail="message images exceed maximum total allowed size",
            )
    if request.content:
        return request
    text_content = "\n".join(
        part.text for part in request.parts if isinstance(part, TextPart) and part.text
    )
    return request.model_copy(update={"content": text_content})


async def _read_limited_request_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length header") from exc
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length header")
        if declared_size > max_bytes:
            raise HTTPException(status_code=400, detail="image exceeds maximum allowed size")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=400, detail="image exceeds maximum allowed size")
        body.extend(chunk)
    return bytes(body)


def _enforce_image_dimension_limits(
    data: bytes,
    mime_type: str,
    settings: ImageInputSettings,
) -> None:
    try:
        enforce_image_dimensions(
            data,
            mime_type,
            max_pixels=settings.max_pixels,
            max_dimension=settings.max_dimension,
        )
    except ImageDimensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _image_bytes_match_mime_type(data: bytes, mime_type: str) -> bool:
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if mime_type in {"image/avif", "image/heif", "image/heic"}:
        return (
            len(data) >= 12
            and data[4:8] == b"ftyp"
            and any(brand in data[8:32] for brand in (b"avif", b"avis", b"heic", b"heix", b"mif1"))
        )
    # Custom configured image MIME types remain permitted when Minigent has no
    # signature matcher for them.
    return True


async def _monitor_distributed_run(
    task: asyncio.Task[Any],
    store: ThreadStore,
    tenant_id: str,
    thread_id: str,
) -> None:
    run_id: str | None = None
    while not task.done():
        await asyncio.sleep(1.0)
        if task.done():
            return
        if run_id is None:
            run_id = store.owned_run_id(tenant_id, thread_id)
            if run_id is None:
                continue
        active = store.heartbeat_run(
            tenant_id,
            thread_id,
            run_id=run_id,
            lease_seconds=DEFAULT_RUN_LEASE_SECONDS,
        )
        if active:
            if store.run_cancellation_requested(tenant_id, thread_id, run_id=run_id):
                task.cancel()
                return
        else:
            task.cancel()
            return


def _instance_accepting_runs(request: Request) -> bool:
    return bool(request.app.state.accepting_runs)


def _reject_if_draining(request: Request) -> None:
    if not _instance_accepting_runs(request):
        raise HTTPException(status_code=503, detail="Instance is draining")


async def _drain_active_runs(app: FastAPI) -> int:
    app.state.accepting_runs = False
    # Let request handlers that were admitted before the flag changed register their backend task.
    await asyncio.sleep(0)
    tasks = [task for task in app.state.active_run_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


async def _await_backend_run(
    request: Request, principal: Principal, thread_id: str
) -> tuple[str, dict[str, Any] | None]:
    _reject_if_draining(request)
    run_key = (principal.tenant_id, thread_id)
    task = asyncio.create_task(request.app.state.agent_backend.run_thread(principal, thread_id))
    request.app.state.active_run_tasks[run_key] = task
    monitor = asyncio.create_task(
        _monitor_distributed_run(task, request.app.state.store, principal.tenant_id, thread_id)
    )
    try:
        return await task
    finally:
        if request.app.state.active_run_tasks.get(run_key) is task:
            request.app.state.active_run_tasks.pop(run_key, None)
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass


async def _run_thread_ndjson_stream(
    request: Request,
    principal: Principal,
    thread_id: str,
) -> AsyncIterator[str]:
    if not _instance_accepting_runs(request):
        yield _ndjson_event(
            {"type": "run.error", "status_code": 503, "detail": "Instance is draining"}
        )
        return
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

    async def emit(event: dict[str, object]) -> None:
        await queue.put({"thread_id": thread_id, **event})

    async def run() -> None:
        started_event: dict[str, object] = {"type": "run.started"}
        started_context = _thread_context_usage(request, principal, thread_id)
        if started_context is not None:
            started_event["thread_context"] = started_context
        await emit(started_event)
        try:
            reply, metadata = await request.app.state.agent_backend.run_thread(
                principal,
                thread_id,
                event_sink=emit,
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("code") == "max_iterations":
                await emit(
                    {
                        "type": "run.warning",
                        "status_code": exc.status_code,
                        "detail": detail.get("message", "Reached tool call limit."),
                    }
                )
                return
            await emit(
                {
                    "type": "run.error",
                    "status_code": exc.status_code,
                    "detail": detail,
                }
            )
            return
        except Exception as exc:  # pragma: no cover - defensive streaming boundary
            await emit({"type": "run.error", "status_code": 500, "detail": str(exc)})
            return
        event: dict[str, object] = {"type": "assistant.message", "content": reply}
        if metadata:
            event["metadata"] = metadata
        await emit(event)
        await emit(
            {
                "type": "run.completed",
                "thread_context": _thread_context_usage(request, principal, thread_id),
            }
        )

    run_key = (principal.tenant_id, thread_id)
    task = asyncio.create_task(run())
    task.add_done_callback(lambda _task: queue.put_nowait(None))
    request.app.state.active_run_tasks[run_key] = task
    monitor = asyncio.create_task(
        _monitor_distributed_run(
            task,
            request.app.state.store,
            principal.tenant_id,
            thread_id,
        )
    )
    try:
        while True:
            if task.done() and queue.empty():
                break
            event = await queue.get()
            if event is not None:
                yield _ndjson_event(event)
    finally:
        if request.app.state.active_run_tasks.get(run_key) is task:
            request.app.state.active_run_tasks.pop(run_key, None)
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _ndjson_event(event: dict[str, object]) -> str:
    return json.dumps(event, ensure_ascii=True) + "\n"


def _thread_context_usage(
    request: Request,
    principal: Principal,
    thread_id: str,
) -> dict[str, int | bool] | None:
    try:
        store = request.app.state.store
        return estimate_thread_context_usage(
            store.list_messages(principal.tenant_id, thread_id),
            context=store.get_thread_context(principal.tenant_id, thread_id),
        )
    except HTTPException:
        return None


def create_app(
    llm_adapter: LLMAdapter | None = None,
    tool_registry: ToolRegistry | None = None,
    execution_resolver: TenantExecutionResolver | None = None,
    admin_store: SQLiteTenantConfigStore | None = None,
    tenant_config_source: str | None = None,
    peer_agent_registry: PeerAgentRegistry | None = None,
    thread_store: ThreadStore | None = None,
    attachment_store: AttachmentStore | None = None,
    rate_limiter: RateLimiter | None = None,
    settings: MinigentSettings | None = None,
) -> FastAPI:
    settings_was_provided = settings is not None
    settings = settings or load_settings()
    validate_auth_settings()
    session_auth_settings = validate_session_auth_settings()
    mcp_manager = (
        MCPServerManager() if execution_resolver is None and tool_registry is None else None
    )
    admin_mcp_server = build_admin_mcp_server()
    admin_mcp_app = admin_mcp_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
    )
    admin_mcp_lifespan = admin_mcp_server.session_manager.run()
    user_mcp_server = build_user_mcp_server()
    user_mcp_app = user_mcp_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
    )
    user_mcp_lifespan = user_mcp_server.session_manager.run()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await admin_mcp_lifespan.__aenter__()
        await user_mcp_lifespan.__aenter__()
        if mcp_manager is not None:
            await mcp_manager.start()
        _log_available_internal_tools(app.state.execution_resolver)
        stale_recovery_task = asyncio.create_task(
            _recover_stale_runs_periodically(
                app.state.store,
                app.state.peer_agent_registry,
            )
        )
        attachment_cleanup_task = asyncio.create_task(
            _cleanup_pending_attachments_periodically(
                app.state.attachment_store,
                interval_seconds=app.state.attachment_store_settings.cleanup_interval_seconds,
            )
        )
        deprovisioning_processor = app.state.user_deprovisioning_processor
        deprovisioning_task = (
            asyncio.create_task(
                deprovisioning_processor.run(),
                name="user-deprovisioning-worker",
            )
            if deprovisioning_processor is not None
            else None
        )
        try:
            yield
        finally:
            await _drain_active_runs(app)
            stale_recovery_task.cancel()
            attachment_cleanup_task.cancel()
            if deprovisioning_task is not None:
                deprovisioning_task.cancel()
            for task in (stale_recovery_task, attachment_cleanup_task, deprovisioning_task):
                if task is None:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if mcp_manager is not None:
                await mcp_manager.stop()
            await user_mcp_lifespan.__aexit__(None, None, None)
            await admin_mcp_lifespan.__aexit__(None, None, None)

    app = FastAPI(title="Minimal AI Agent Runtime", version="0.1.0", lifespan=lifespan)
    app.state.session_auth_settings = session_auth_settings
    app.add_middleware(SecurityHeadersMiddleware)
    configure_tracing(app, settings.tracing)
    if thread_store is not None:
        app.state.store = thread_store
    elif settings_was_provided:
        app.state.store = build_thread_store(settings.thread_store)
    else:
        app.state.store = build_thread_store_from_env()
    app.state.store.recover_stale_runs()
    app.state.attachment_store_settings = settings.attachment_store
    app.state.attachment_store = attachment_store or build_attachment_store(
        settings.attachment_store
    )
    app.state.rate_limit_settings = settings.rate_limits
    app.state.rate_limiter = rate_limiter or build_rate_limiter(settings.rate_limits)
    app.state.mcp_manager = mcp_manager
    app.state.mcp_broker_sessions = build_mcp_broker_session_store_from_env()
    app.state.oauth_flows = build_oauth_flow_store_from_env()
    admin_store_settings = settings.admin_store
    app.state.admin_store_settings = admin_store_settings
    admin_encryption_key = admin_store_settings.encryption_key
    if admin_store is None:
        admin_db_path = admin_store_settings.db_path
        if admin_db_path is not None:
            admin_store = SQLiteTenantConfigStore(
                admin_db_path,
                encryption_key=admin_encryption_key,
            )
    app.state.admin_store = admin_store
    app.state.external_grant_provider_registry = build_external_grant_provider_registry_from_env()
    app.state.user_deprovisioning_processor = (
        UserDeprovisioningProcessor(admin_store, app.state.external_grant_provider_registry)
        if admin_store is not None
        else None
    )
    admin_fallback_execution_resolver: TenantExecutionResolver | None = None
    if execution_resolver is None:
        if llm_adapter is not None or tool_registry is not None:
            adapter = llm_adapter or build_llm_adapter_from_env()
            registry = tool_registry or build_tool_registry_from_env()
            execution_resolver = FixedTenantExecutionResolver(adapter, registry)
        else:
            config_source = resolve_tenant_config_source(tenant_config_source)
            fallback_resolver = build_execution_resolver_from_env(mcp_manager=mcp_manager)
            admin_fallback_execution_resolver = fallback_resolver
            if config_source == TENANT_CONFIG_SOURCE_ENV_ONLY:
                execution_resolver = fallback_resolver
            elif config_source == TENANT_CONFIG_SOURCE_STORE:
                if admin_store is None:
                    raise RuntimeError(
                        "MINIGENT_ADMIN_DB_PATH or admin_store is required when "
                        "MINIGENT_TENANT_CONFIG_SOURCE=store"
                    )
                if admin_encryption_key is None and admin_store_settings.db_path is not None:
                    raise RuntimeError(
                        "MINIGENT_ADMIN_ENCRYPTION_KEY is required when "
                        "MINIGENT_TENANT_CONFIG_SOURCE=store"
                    )
                execution_resolver = StoreBackedTenantExecutionResolver(
                    admin_store,
                    mcp_manager=mcp_manager,
                    mcp_server_catalog={
                        item.id: item.server for item in admin_store_settings.mcp_server_catalog
                    },
                )
            elif config_source == TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS:
                if admin_store is None:
                    raise RuntimeError(
                        "MINIGENT_ADMIN_DB_PATH or admin_store is required when "
                        "MINIGENT_TENANT_CONFIG_SOURCE=store-with-defaults"
                    )
                if admin_encryption_key is None and admin_store_settings.db_path is not None:
                    raise RuntimeError(
                        "MINIGENT_ADMIN_ENCRYPTION_KEY is required when "
                        "MINIGENT_TENANT_CONFIG_SOURCE=store-with-defaults"
                    )
                execution_resolver = StoreBackedTenantExecutionResolver(
                    admin_store,
                    fallback_resolver=fallback_resolver,
                    mcp_manager=mcp_manager,
                    mcp_server_catalog={
                        item.id: item.server for item in admin_store_settings.mcp_server_catalog
                    },
                )
            else:
                raise RuntimeError(f"Unhandled tenant config source '{config_source}'")
    deployment_execution_resolver = admin_fallback_execution_resolver or execution_resolver
    admin_execution_resolver: TenantExecutionResolver = deployment_execution_resolver
    if admin_store is not None:
        admin_execution_resolver = StoreBackedTenantExecutionResolver(
            admin_store,
            fallback_resolver=deployment_execution_resolver,
            mcp_manager=mcp_manager,
            mcp_server_catalog={
                item.id: item.server for item in admin_store_settings.mcp_server_catalog
            },
        )
    app.state.execution_resolver = execution_resolver
    app.state.admin_execution_resolver = admin_execution_resolver
    app.state.quality_enhancer = QualityEnhancer()
    runtime_settings = settings.runtime
    app.state.image_input_settings = settings.image_input
    app.state.runtime_settings = runtime_settings
    mcp_server_name_authorizer = None
    if admin_store is not None and admin_store_settings.mcp_server_catalog:
        catalog_server_names = {
            item.id: str(item.server["name"])
            for item in admin_store_settings.mcp_server_catalog
            if isinstance(item.server.get("name"), str)
        }

        def authorize_mcp_server_names(tenant_id: str, user_id: str) -> set[str] | None:
            item_ids = admin_store.effective_subject_mcp_server_catalog_item_ids(tenant_id, user_id)
            if item_ids is None:
                return None
            return {
                catalog_server_names[item_id]
                for item_id in item_ids
                if item_id in catalog_server_names
            }

        mcp_server_name_authorizer = authorize_mcp_server_names

    def resolve_principal_execution(principal: Principal) -> TenantExecutionContext:
        if principal.is_admin:
            return admin_execution_resolver.resolve(ADMIN_EXECUTION_CONFIG_KEY)
        return execution_resolver.resolve(principal.tenant_id)

    def principal_tool_registry_provider(principal: Principal) -> ToolRegistry | None:
        if principal.is_admin:
            return build_admin_chat_tool_registry(app, principal)
        return build_user_mcp_tool_registry(app, principal)

    app.state.runtime = AgentRuntime(
        store=app.state.store,
        execution_resolver=execution_resolver,
        max_iterations=runtime_settings.max_iterations,
        tool_timeout_seconds=runtime_settings.tool_timeout_seconds,
        quality_enhancer=app.state.quality_enhancer,
        context_compaction_enabled=runtime_settings.context_compaction_enabled,
        attachment_store=app.state.attachment_store,
        mcp_server_name_authorizer=mcp_server_name_authorizer,
        user_execution_config_source=admin_store,
        principal_execution_resolver=resolve_principal_execution,
        principal_tool_registry_provider=principal_tool_registry_provider,
    )
    app.state.peer_agent_registry = (
        peer_agent_registry
        if peer_agent_registry is not None
        else build_peer_agent_registry(settings.peer_agents)
    )
    app.state.active_run_tasks = {}
    app.state.accepting_runs = True
    app.state.agent_backend = AgentBackendRouter(
        store=app.state.store,
        execution_resolver=execution_resolver,
        native_backend=NativeAgentBackend(app.state.runtime),
        peer_agent_registry=app.state.peer_agent_registry,
        mcp_broker_sessions=app.state.mcp_broker_sessions,
        mcp_server_name_authorizer=mcp_server_name_authorizer,
        user_execution_config_source=admin_store,
        principal_execution_resolver=resolve_principal_execution,
        principal_tool_registry_provider=principal_tool_registry_provider,
    )
    app.include_router(build_session_auth_router())
    app.include_router(build_admin_router())
    app.include_router(build_user_execution_router())
    if CONSOLE_CLIENT_DIR.exists():
        app.mount(
            "/console",
            StaticFiles(directory=CONSOLE_CLIENT_DIR, html=True),
            name="console",
        )
    if WEB_CLIENT_DIR.exists():
        app.mount("/web", StaticFiles(directory=WEB_CLIENT_DIR, html=True), name="web")

    @app.get("/health")
    @app.get("/health/live")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=None)
    async def readiness() -> dict[str, object] | JSONResponse:
        checks = await database_readiness_checks()
        session_settings = app.state.session_auth_settings
        if (
            session_settings.enabled
            and (
                not session_settings.credentials
                or any(
                    credential.principal.is_admin
                    for credential in session_settings.credentials.values()
                )
            )
            and app.state.admin_store is None
        ):
            checks["admin_store"] = False
        if not app.state.accepting_runs:
            checks["lifecycle"] = False
        ready = all(checks.values())
        payload: dict[str, object] = {
            "status": "ready" if ready else "not_ready",
            "checks": {name: "ok" if value else "failed" for name, value in checks.items()},
        }
        if not ready:
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.post("/health/drain")
    async def drain(request: Request) -> dict[str, object]:
        client_host = request.client.host if request.client is not None else None
        if client_host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=403, detail="Drain endpoint is loopback-only")
        cancelled_runs = await _drain_active_runs(request.app)
        return {"status": "draining", "cancelled_runs": cancelled_runs}

    @app.get("/config")
    async def config(request: Request, export: bool = False) -> dict[str, object]:
        result = request.app.state.execution_resolver.describe(include_export=export)
        image_input_settings = request.app.state.image_input_settings
        result["image_input"] = _image_input_public_dict(image_input_settings)
        if export:
            image_input_export = _image_input_export_public_dict(image_input_settings)
            unified_export = result.get("unified_config_export")
            if image_input_export and isinstance(unified_export, dict):
                unified_export["image_input"] = image_input_export
        return result

    @app.get("/execution-options", response_model=ExecutionOptionsResponse)
    async def execution_options(
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> ExecutionOptionsResponse:
        execution = resolve_principal_execution(principal)
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        config = execution.config
        catalog = effective_execution_catalog(
            config,
            request.app.state.admin_store,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        )
        skill_options = catalog.skill_options()
        capability_options = catalog.capability_profile_options()
        agent_options = catalog.agent_options()
        default_skill_refs = catalog.default_skill_refs
        return ExecutionOptionsResponse(
            tenant_id=principal.tenant_id,
            skills=ExecutionOptionSection(
                default=(default_skill_refs[0] if default_skill_refs else None),
                defaults=default_skill_refs,
                items=[
                    ExecutionOptionItem(
                        name=(option.display_name if option.source == "shared" else option.id),
                        id=option.id,
                        display_name=option.display_name,
                        description=option.description,
                        source=option.source,
                        version=option.version,
                    )
                    for option in skill_options
                ],
            ),
            capability_profiles=ExecutionOptionSection(
                default=catalog.default_capability_profile_ref,
                items=[
                    ExecutionOptionItem(
                        name=(option.display_name if option.source == "shared" else option.id),
                        id=option.id,
                        display_name=option.display_name,
                        description=option.description,
                        source=option.source,
                        version=option.version,
                    )
                    for option in capability_options
                ],
            ),
            llm_profiles=ExecutionOptionSection(
                default=config.default_llm_profile,
                items=[
                    ExecutionOptionItem(
                        name=name,
                        id=f"shared:{name}",
                        display_name=name,
                        source="shared",
                    )
                    for name in config.llm_profiles
                ],
            ),
            agents=ExecutionAgentOptionSection(
                default=catalog.default_agent_ref,
                items=[
                    ExecutionAgentOptionItem(
                        name=(option.display_name if option.source == "shared" else option.id),
                        id=option.id,
                        display_name=option.display_name,
                        description=option.description,
                        source=option.source,
                        version=option.version,
                        skill_name=(
                            option.skill_refs[0]
                            if len(option.skill_refs) == 1 and not option.uses_skill_list
                            else None
                        ),
                        skills=(list(option.skill_refs) if option.uses_skill_list else None),
                        capability_profile=option.capability_profile_ref,
                        llm_profile=option.llm_profile,
                    )
                    for option in agent_options
                ],
            ),
        )

    @app.get("/tenant-context")
    async def tenant_context(
        context: TenantContext = Depends(require_tenant_context),
    ) -> dict[str, object]:
        return context.model_dump(mode="json")

    @app.get("/oauth/generic/login")
    async def generic_oauth_login(request: Request) -> dict[str, str]:
        oauth_provider = GenericOAuthProvider()
        login, flow = oauth_provider.start_login()
        request.app.state.oauth_flows.put(login.state, flow)
        return {
            "provider": oauth_provider.provider_id,
            "authorization_url": login.authorization_url,
            "state": login.state,
            "instructions": "Open authorization_url in a browser, complete OAuth login, then return to Minigent.",
        }

    @app.get("/oauth/generic/callback", response_class=HTMLResponse)
    @app.get("/auth/callback", response_class=HTMLResponse)
    async def generic_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        if error:
            return HTMLResponse(f"<h1>OAuth login failed</h1><p>{error}</p>", status_code=400)
        if not code or not state:
            return HTMLResponse(
                "<h1>OAuth login failed</h1><p>Missing code or state.</p>",
                status_code=400,
            )
        flow = request.app.state.oauth_flows.pop(state)
        if flow is None:
            return HTMLResponse(
                "<h1>OAuth login failed</h1><p>Unknown or expired OAuth state.</p>",
                status_code=400,
            )
        try:
            await GenericOAuthProvider().complete_login(code=code, flow=flow)
        except Exception as exc:
            logger.warning("Generic OAuth callback failed: %s", exc)
            return HTMLResponse(
                "<h1>OAuth login failed</h1><p>Token exchange failed.</p>",
                status_code=502,
            )
        return HTMLResponse("<h1>OAuth login complete</h1><p>You can close this tab.</p>")

    @app.get("/oauth/generic/open")
    async def generic_oauth_open(request: Request) -> RedirectResponse:
        oauth_provider = GenericOAuthProvider()
        login, flow = oauth_provider.start_login()
        request.app.state.oauth_flows.put(login.state, flow)
        return RedirectResponse(login.authorization_url)

    @app.get("/peer-agents")
    async def peer_agents(request: Request) -> dict[str, object]:
        return {"agents": await request.app.state.peer_agent_registry.list_agents_with_cards()}

    @app.post("/mcp/peer/{session_id}", response_model=None)
    async def mcp_peer_broker(session_id: str, request: Request):
        def resolve_tool_registry(session: MCPBrokerSession) -> ToolRegistry:
            principal = Principal(user_id=session.user_id, tenant_id=session.tenant_id)
            return request.app.state.agent_backend.tool_registry_for_thread(
                principal, session.thread_id
            )

        return await handle_mcp_broker_request(
            session_store=request.app.state.mcp_broker_sessions,
            session_id=session_id,
            request=request,
            tool_registry_resolver=resolve_tool_registry,
        )

    @app.get("/peer-agents/{name}/agent-card")
    async def peer_agent_card(name: str, request: Request) -> dict[str, object]:
        return await request.app.state.peer_agent_registry.agent_card(name)

    @app.post("/peer-agents/{name}/tasks")
    async def create_peer_agent_task(
        name: str,
        body: dict[str, Any],
        request: Request,
    ) -> dict[str, object]:
        return await request.app.state.peer_agent_registry.create_task(name, body)

    @app.get("/peer-agents/{name}/tasks/{task_id}")
    async def peer_agent_task(
        name: str,
        task_id: str,
        request: Request,
    ) -> dict[str, object]:
        return await request.app.state.peer_agent_registry.task(name, task_id)

    @app.post("/peer-agents/{name}/tasks/{task_id}/cancel")
    async def cancel_peer_agent_task(
        name: str,
        task_id: str,
        request: Request,
    ) -> dict[str, object]:
        return await request.app.state.peer_agent_registry.cancel_task(name, task_id)

    @app.get("/peer-agents/{name}/tasks/{task_id}/events")
    async def peer_agent_task_events(
        name: str,
        task_id: str,
        request: Request,
        after: int | None = None,
    ) -> dict[str, object]:
        return await request.app.state.peer_agent_registry.task_events(name, task_id, after=after)

    @app.get("/peer-agents/{name}/tasks/{task_id}/artifacts/{artifact_name}")
    async def peer_agent_task_artifact(
        name: str,
        task_id: str,
        artifact_name: str,
        request: Request,
    ) -> Response:
        artifact = await request.app.state.peer_agent_registry.task_artifact(
            name,
            task_id,
            artifact_name,
        )
        return Response(content=artifact.content, media_type=artifact.media_type)

    @app.get("/threads", response_model=ThreadListResponse)
    async def list_threads(
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
        limit: int = 50,
        offset: int = 0,
    ) -> ThreadListResponse:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be greater than or equal to 0")
        store = request.app.state.store
        threads = store.list_threads(principal.tenant_id, limit=limit, offset=offset)
        return ThreadListResponse(
            threads=[_thread_list_item(store, principal.tenant_id, thread) for thread in threads],
            total=store.count_threads(principal.tenant_id),
            limit=limit,
            offset=offset,
        )

    @app.patch("/threads/{thread_id}/title", response_model=ThreadTitleResponse)
    async def update_thread_title(
        thread_id: str,
        body: UpdateThreadTitleRequest,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> ThreadTitleResponse:
        try:
            title = normalize_manual_thread_title(body.title)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        thread = request.app.state.store.set_thread_title(
            principal.tenant_id,
            thread_id,
            title=title,
            source="manual",
        )
        assert thread.title is not None
        assert thread.title_source is not None
        assert thread.title_updated_at is not None
        return ThreadTitleResponse(
            thread_id=thread.thread_id,
            title=thread.title,
            title_source=thread.title_source,
            title_updated_at=thread.title_updated_at,
        )

    @app.post("/threads", response_model=CreateThreadResponse)
    async def create_thread(
        request: Request,
        body: CreateThreadRequest | None = None,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> CreateThreadResponse:
        agent_name = body.agent_name if body is not None else None
        skill_name = body.skill_name if body is not None else None
        skill_names = body.skill_names if body is not None else None
        capability_profile = body.capability_profile if body is not None else None
        llm_profile = body.llm_profile if body is not None else None
        execution = resolve_principal_execution(principal)
        enforce_thread_creation_limit(
            context=tenant_context_from_request_state(request.state),
            store=request.app.state.store,
        )
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        catalog = effective_execution_catalog(
            execution.config,
            request.app.state.admin_store,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        )
        try:
            agent = catalog.resolve_agent(agent_name, use_default=True)
        except UserExecutionResolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if skill_name is not None and skill_names is not None:
            raise HTTPException(
                status_code=400,
                detail="Provide either skill_name or skill_names, not both",
            )
        explicit_skill_names = skill_names is not None
        explicit_skill_name = skill_name is not None
        requested_skill_refs: list[str] | None
        if skill_names is not None:
            duplicates = sorted({name for name in skill_names if skill_names.count(name) > 1})
            if duplicates:
                raise HTTPException(
                    status_code=400,
                    detail="Duplicate skill_names are not allowed: " + ", ".join(duplicates),
                )
            requested_skill_refs = skill_names
        elif skill_name is not None:
            requested_skill_refs = [skill_name]
        elif agent is not None:
            requested_skill_refs = list(agent.skill_refs) or catalog.default_skill_refs
        else:
            requested_skill_refs = catalog.default_skill_refs
        try:
            resolved_skills = catalog.resolve_skill_refs(
                requested_skill_refs,
                use_defaults=False,
            )
        except UserExecutionResolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        skill_names = [skill.stored_ref for skill in resolved_skills]
        if not skill_names:
            skill_names = [] if explicit_skill_names else None
            skill_name = None
        elif explicit_skill_names:
            skill_name = None
        elif explicit_skill_name or len(skill_names) == 1:
            skill_name = skill_names[0]

        requested_capability_ref = capability_profile
        if requested_capability_ref is None and agent is not None:
            requested_capability_ref = agent.capability_profile_ref
        if requested_capability_ref is None:
            requested_capability_ref = catalog.default_capability_profile_ref
        try:
            resolved_capability = catalog.resolve_capability_profile(
                requested_capability_ref,
                use_default=False,
            )
        except UserExecutionResolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if resolved_capability is not None and resolved_capability.source == "user":
            try:
                catalog.personal_capability_constraints(resolved_capability)
            except UserExecutionResolutionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        capability_profile = (
            resolved_capability.stored_ref if resolved_capability is not None else None
        )
        if llm_profile is None and agent is not None:
            llm_profile = agent.llm_profile
        if llm_profile is None:
            llm_profile = execution.config.default_llm_profile
        if llm_profile is not None and llm_profile not in execution.config.llm_profiles:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown LLM profile '{llm_profile}' for tenant '{principal.tenant_id}'",
            )
        thread = request.app.state.store.create_thread(
            principal.tenant_id,
            execution_user_id=principal.user_id,
            skill_name=skill_name,
            skill_names=skill_names,
            capability_profile=capability_profile,
            llm_profile=llm_profile,
        )
        return CreateThreadResponse(thread_id=thread.thread_id)

    def persist_image_attachment(
        app_request: Request,
        principal: Principal,
        thread_id: str,
        *,
        mime_type: str,
        data: bytes,
    ) -> AttachmentMetadata:
        app_request.app.state.store.get_thread(principal.tenant_id, thread_id)
        image_settings = app_request.app.state.image_input_settings
        if not image_settings.enabled:
            raise HTTPException(status_code=400, detail="image input is disabled")
        normalized_mime_type = mime_type.strip().lower().split(";", 1)[0]
        if normalized_mime_type not in image_settings.allowed_mime_types:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported image MIME type: {normalized_mime_type}",
            )
        if len(data) > image_settings.max_bytes:
            raise HTTPException(status_code=400, detail="image exceeds maximum allowed size")
        if not _image_bytes_match_mime_type(data, normalized_mime_type):
            raise HTTPException(
                status_code=400,
                detail=f"image data does not match declared MIME type: {normalized_mime_type}",
            )
        _enforce_image_dimension_limits(data, normalized_mime_type, image_settings)
        attachment_settings = app_request.app.state.attachment_store_settings
        attachment_store = app_request.app.state.attachment_store
        _cleanup_pending_attachments_before_upload(attachment_store)
        try:
            return attachment_store.put(
                principal.tenant_id,
                thread_id,
                mime_type=normalized_mime_type,
                data=data,
                created_by=principal.user_id,
                max_per_thread=attachment_settings.max_per_thread,
                max_bytes_per_thread=attachment_settings.max_bytes_per_thread,
                max_per_tenant=attachment_settings.max_per_tenant,
                max_bytes_per_tenant=attachment_settings.max_bytes_per_tenant,
                pending_ttl_seconds=attachment_settings.pending_ttl_seconds,
            )
        except AttachmentLimitExceeded as exc:
            logger.warning(
                "attachment.quota_rejected tenant_id=%s thread_id=%s limit=%s incoming_bytes=%s",
                principal.tenant_id,
                thread_id,
                exc.limit,
                len(data),
            )
            detail = {
                "count": "thread attachment count limit exceeded",
                "bytes": "thread attachment storage limit exceeded",
                "tenant_count": "tenant attachment count limit exceeded",
                "tenant_bytes": "tenant attachment storage limit exceeded",
            }[exc.limit]
            raise HTTPException(status_code=400, detail=detail) from exc

    @app.post(
        "/threads/{thread_id}/attachments",
        response_model=AttachmentMetadata,
    )
    async def upload_attachment(
        thread_id: str,
        upload: UploadAttachmentRequest,
        app_request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> AttachmentMetadata:
        _enforce_request_rate_limit(app_request, principal, UPLOAD_RATE_LIMIT_CATEGORY)
        app_request.app.state.store.get_thread(principal.tenant_id, thread_id)
        try:
            data = base64.b64decode(upload.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="image data must be base64") from exc
        return persist_image_attachment(
            app_request,
            principal,
            thread_id,
            mime_type=upload.mime_type,
            data=data,
        )

    @app.post(
        "/threads/{thread_id}/attachments/binary",
        response_model=AttachmentMetadata,
    )
    async def upload_binary_attachment(
        thread_id: str,
        app_request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> AttachmentMetadata:
        _enforce_request_rate_limit(app_request, principal, UPLOAD_RATE_LIMIT_CATEGORY)
        app_request.app.state.store.get_thread(principal.tenant_id, thread_id)
        image_settings = app_request.app.state.image_input_settings
        if not image_settings.enabled:
            raise HTTPException(status_code=400, detail="image input is disabled")
        mime_type = app_request.headers.get("content-type", "").strip().lower().split(";", 1)[0]
        if mime_type not in image_settings.allowed_mime_types:
            raise HTTPException(status_code=400, detail=f"unsupported image MIME type: {mime_type}")
        data = await _read_limited_request_body(app_request, image_settings.max_bytes)
        return persist_image_attachment(
            app_request,
            principal,
            thread_id,
            mime_type=mime_type,
            data=data,
        )

    @app.get("/threads/{thread_id}/attachments/{attachment_id}")
    async def get_attachment(
        thread_id: str,
        attachment_id: str,
        app_request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> Response:
        app_request.app.state.store.get_thread(principal.tenant_id, thread_id)
        record = app_request.app.state.attachment_store.get(
            principal.tenant_id,
            thread_id,
            attachment_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        return Response(
            content=record.data,
            media_type=record.metadata.mime_type,
            headers={"Cache-Control": "private, max-age=300"},
        )

    @app.delete("/threads/{thread_id}/attachments/{attachment_id}", status_code=204)
    async def delete_attachment(
        thread_id: str,
        attachment_id: str,
        app_request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> None:
        app_request.app.state.store.get_thread(principal.tenant_id, thread_id)
        messages = app_request.app.state.store.list_messages(principal.tenant_id, thread_id)
        if any(
            part.type == "image" and part.attachment_id == attachment_id
            for message in messages
            for part in (message.parts or [])
        ):
            raise HTTPException(
                status_code=409, detail="attachment is referenced by message history"
            )
        deleted = app_request.app.state.attachment_store.delete_unreferenced(
            principal.tenant_id,
            thread_id,
            attachment_id,
        )
        if not deleted:
            existing = app_request.app.state.attachment_store.get(
                principal.tenant_id,
                thread_id,
                attachment_id,
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="attachment is referenced by message history",
                )
            raise HTTPException(status_code=404, detail="attachment not found")

    @app.post("/threads/{thread_id}/messages", response_model=Message)
    async def add_message(
        thread_id: str,
        request: AddMessageRequest,
        app_request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> Message:
        enforce_message_creation_limit(
            context=tenant_context_from_request_state(app_request.state),
            store=app_request.app.state.store,
            thread_id=thread_id,
        )
        execution = resolve_principal_execution(principal)
        thread = app_request.app.state.store.get_thread(principal.tenant_id, thread_id)
        llm_config = get_llm_config(execution, thread.llm_profile)
        request = _validate_and_normalize_message_request(
            request,
            app_request.app.state.image_input_settings,
            input_modalities=llm_config.input_modalities,
            attachment_store=app_request.app.state.attachment_store,
            tenant_id=principal.tenant_id,
            thread_id=thread_id,
        )
        protected_content = await app_request.app.state.runtime.protect_user_content(
            principal,
            thread_id,
            request.content,
        )
        protected_parts = None
        if request.parts is not None:
            protected_parts = []
            for part in request.parts:
                if isinstance(part, TextPart):
                    protected_parts.append(
                        part.model_copy(
                            update={
                                "text": await app_request.app.state.runtime.protect_user_content(
                                    principal,
                                    thread_id,
                                    part.text,
                                )
                            }
                        )
                    )
                else:
                    protected_parts.append(part)
        attachment_ids = [
            part.attachment_id
            for part in (protected_parts or [])
            if part.type == "image" and part.attachment_id is not None
        ]
        marked_attachment_ids: list[str] = []
        for attachment_id in attachment_ids:
            marked = app_request.app.state.attachment_store.mark_referenced(
                principal.tenant_id,
                thread_id,
                attachment_id,
            )
            if not marked:
                for marked_id in marked_attachment_ids:
                    app_request.app.state.attachment_store.unmark_referenced(
                        principal.tenant_id,
                        thread_id,
                        marked_id,
                    )
                raise HTTPException(status_code=400, detail="image attachment_id is invalid")
            marked_attachment_ids.append(attachment_id)
        try:
            stored_message = app_request.app.state.store.append_message(
                principal.tenant_id,
                Message(
                    thread_id=thread_id,
                    role=MessageRole.USER,
                    content=protected_content,
                    parts=protected_parts,
                    created_by=principal.user_id,
                    metadata=request.metadata,
                ),
            )
        except Exception:
            for attachment_id in marked_attachment_ids:
                app_request.app.state.attachment_store.unmark_referenced(
                    principal.tenant_id,
                    thread_id,
                    attachment_id,
                )
            raise
        return app_request.app.state.runtime.render_messages_for_user(
            principal,
            thread_id,
            [stored_message],
        )[0]

    @app.get("/threads/{thread_id}/messages", response_model=list[Message])
    async def get_messages(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> list[Message]:
        messages = request.app.state.store.list_messages(principal.tenant_id, thread_id)
        return request.app.state.runtime.render_messages_for_user(
            principal,
            thread_id,
            messages,
        )

    @app.get("/threads/{thread_id}/context/raw")
    async def get_raw_thread_context(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> dict[str, object]:
        store = request.app.state.store
        messages = store.list_messages(principal.tenant_id, thread_id)
        context = store.get_thread_context(principal.tenant_id, thread_id)
        return {
            "thread_id": thread_id,
            "summary": context.summary,
            "summarized_message_count": context.summarized_message_count,
            "messages": [message.model_dump(mode="json") for message in messages],
            "rendered": render_raw_thread_context(messages, context=context),
            "usage": estimate_thread_context_usage(messages, context=context),
        }

    @app.post("/threads/{thread_id}/compact")
    async def compact_thread_context(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> dict[str, object]:
        store = request.app.state.store
        before_messages = store.list_messages(principal.tenant_id, thread_id)
        before_context = store.get_thread_context(principal.tenant_id, thread_id)
        before_usage = estimate_thread_context_usage(before_messages, context=before_context)
        context = request.app.state.runtime.compact_thread(principal, thread_id)
        after_messages = store.list_messages(principal.tenant_id, thread_id)
        after_usage = estimate_thread_context_usage(after_messages, context=context)
        return {
            "thread_id": thread_id,
            "summary": context.summary,
            "compacted_message_count": len(before_messages) - len(after_messages),
            "message_count": len(after_messages),
            "usage_before": before_usage,
            "usage": after_usage,
        }

    @app.post("/threads/{thread_id}/run", response_model=RunThreadResponse)
    async def run_thread(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> RunThreadResponse:
        _reject_if_draining(request)
        _enforce_request_rate_limit(request, principal, RUN_RATE_LIMIT_CATEGORY)
        execution = resolve_principal_execution(principal)
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        enforce_thread_run_limit(
            context=tenant_context_from_request_state(request.state),
            store=request.app.state.store,
            thread_id=thread_id,
        )
        lease = await _acquire_run_concurrency_slot(request, principal)
        async with _maintain_run_concurrency_slot(request.app, lease):
            reply, _metadata = await _await_backend_run(request, principal, thread_id)
        return RunThreadResponse(reply=reply)

    @app.post("/threads/{thread_id}/run/stream", response_model=None)
    async def run_thread_stream(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> StreamingResponse:
        _reject_if_draining(request)
        _enforce_request_rate_limit(request, principal, RUN_RATE_LIMIT_CATEGORY)
        execution = resolve_principal_execution(principal)
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        enforce_thread_run_limit(
            context=tenant_context_from_request_state(request.state),
            store=request.app.state.store,
            thread_id=thread_id,
        )
        lease = await _acquire_run_concurrency_slot(request, principal)
        return StreamingResponse(
            _run_thread_stream_with_concurrency_slot(request, principal, thread_id, lease),
            media_type="application/x-ndjson",
        )

    @app.post("/threads/{thread_id}/run/cancel")
    async def cancel_thread_run(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> dict[str, object]:
        run_key = (principal.tenant_id, thread_id)
        task = request.app.state.active_run_tasks.get(run_key)
        cancelled = request.app.state.store.request_run_cancellation(principal.tenant_id, thread_id)
        if task is not None and not task.done():
            task.cancel()
            cancelled = True
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not cancelled:
            thread = request.app.state.store.get_thread(principal.tenant_id, thread_id)
            if thread.status == ThreadStatus.RUNNING:
                request.app.state.store.set_thread_status(
                    principal.tenant_id, thread_id, ThreadStatus.IDLE
                )
        return {"cancelled": cancelled, "thread_id": thread_id}

    @app.get("/threads/{thread_id}/private-value-consents/pending")
    async def pending_private_value_consents(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> list[dict[str, object]]:
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
        return request.app.state.runtime.pending_private_value_consents(
            principal,
            thread_id,
        )

    @app.post("/threads/{thread_id}/private-value-consents/{consent_id}")
    async def decide_private_value_consent(
        thread_id: str,
        consent_id: str,
        decision: PrivateValueConsentDecisionRequest,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> dict[str, object]:
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
        return request.app.state.runtime.decide_private_value_consent(
            principal,
            thread_id,
            consent_id,
            approve=decision.approve,
            one_shot=decision.one_shot,
        )

    @app.post(
        "/threads/{thread_id}/private-value-consents/{consent_id}/resume",
        response_model=RunThreadResponse,
    )
    async def resume_private_value_consent(
        thread_id: str,
        consent_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> RunThreadResponse:
        _reject_if_draining(request)
        _enforce_request_rate_limit(request, principal, RUN_RATE_LIMIT_CATEGORY)
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
        lease = await _acquire_run_concurrency_slot(request, principal)
        async with _maintain_run_concurrency_slot(request.app, lease):
            reply, _metadata = await request.app.state.runtime.resume_private_value_consent(
                principal,
                thread_id,
                consent_id,
            )
        return RunThreadResponse(reply=reply)

    @app.get("/threads/{thread_id}/private-value-actions")
    async def private_value_action_statuses(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> list[dict[str, object]]:
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
        return request.app.state.runtime.private_value_action_statuses(principal, thread_id)

    @app.delete("/threads/{thread_id}/private-value-actions/{consent_id}")
    async def discard_private_value_action(
        thread_id: str,
        consent_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> dict[str, object]:
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
        return request.app.state.runtime.discard_private_value_action(
            principal,
            thread_id,
            consent_id,
        )

    @app.get("/threads/{thread_id}/private-value-disclosures/audit")
    async def private_value_disclosure_audit(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> list[dict[str, object]]:
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
        return request.app.state.runtime.private_value_disclosure_audit(
            principal,
            thread_id,
        )

    @app.delete("/threads/{thread_id}", status_code=204)
    async def delete_thread(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> None:
        request.app.state.store.delete_thread(principal.tenant_id, thread_id)
        request.app.state.attachment_store.delete_thread(principal.tenant_id, thread_id)
        request.app.state.runtime.clear_private_values(principal, thread_id)

    app.mount("/mcp", AdminMCPAuthMiddleware(admin_mcp_app), name="admin-mcp")
    app.mount("/user-mcp", UserMCPAuthMiddleware(user_mcp_app), name="user-mcp")
    return app


def _log_available_internal_tools(execution_resolver: TenantExecutionResolver) -> None:
    try:
        description = execution_resolver.describe()
    except Exception as exc:
        logger.warning(
            "available_internal_tools.unavailable error_type=%s detail=%s",
            type(exc).__name__,
            exc,
        )
        return

    local_tools = description.get("local_tools", [])
    if not isinstance(local_tools, list):
        local_tools = []
    tenant_id = description.get("tenant_id")
    logger.info(
        "available_internal_tools tenant_id=%s tools=%s count=%s",
        tenant_id,
        sorted(str(tool) for tool in local_tools),
        len(local_tools),
    )


app = create_app()
