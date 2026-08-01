from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.admin_api import build_admin_router
from app.admin_store import SQLiteTenantConfigStore
from app.agent_backends import AgentBackendRouter, NativeAgentBackend
from app.auth import validate_auth_settings
from app.config import load_environment
from app.entitlements import (
    enforce_execution_entitlements,
    enforce_message_creation_limit,
    enforce_thread_creation_limit,
    enforce_thread_run_limit,
    tenant_context_from_request_state,
)
from app.execution import (
    TENANT_CONFIG_SOURCE_ENV_ONLY,
    TENANT_CONFIG_SOURCE_STORE,
    TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS,
    FixedTenantExecutionResolver,
    StoreBackedTenantExecutionResolver,
    TenantExecutionResolver,
    build_execution_resolver_from_env,
    get_capability_profile,
    get_skill_config,
    get_skill_configs,
    resolve_tenant_config_source,
)
from app.health import database_readiness_checks
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
)
from app.oauth import GenericOAuthProvider, build_oauth_flow_store_from_env
from app.observability import configure_logging, configure_tracing
from app.peer_agents import PeerAgentRegistry, build_peer_agent_registry
from app.quality import QualityEnhancer
from app.redaction import install_log_redaction
from app.runtime import (
    AgentRuntime,
    estimate_thread_context_usage,
    render_raw_thread_context,
)
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
from app.tools import ToolRegistry, build_tool_registry_from_env

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
STALE_RUN_RECOVERY_INTERVAL_SECONDS = 5.0
PEER_TASK_CANCELLATION_CLAIM_SECONDS = 30.0
PEER_TASK_CANCELLATION_BATCH_SIZE = 10


def _peer_task_retry_delay(attempts: int) -> float:
    return min(300.0, float(2 ** min(max(attempts, 1), 8)))


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
        title=_thread_title(messages),
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
            return _truncate_thread_title(message.content)
    for message in messages:
        if message.content.strip():
            return _truncate_thread_title(message.content)
    return "New thread"


def _truncate_thread_title(content: str, limit: int = 64) -> str:
    title = " ".join(content.split())
    if len(title) <= limit:
        return title
    return f"{title[: limit - 1].rstrip()}…"


def _validate_and_normalize_message_request(
    request: AddMessageRequest, settings: ImageInputSettings
) -> AddMessageRequest:
    if not request.parts:
        if not request.content:
            raise HTTPException(status_code=400, detail="message content is required")
        return request
    image_parts = [part for part in request.parts if part.type == "image"]
    if image_parts and not settings.enabled:
        raise HTTPException(status_code=400, detail="image input is disabled")
    if len(image_parts) > settings.max_images:
        raise HTTPException(
            status_code=400,
            detail=f"message exceeds maximum image count ({settings.max_images})",
        )
    total_inline_bytes = 0
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
            raise HTTPException(status_code=400, detail="image attachment_id is not supported")
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
            total_inline_bytes += len(decoded)
            if total_inline_bytes > settings.max_total_bytes:
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
    settings: MinigentSettings | None = None,
) -> FastAPI:
    settings_was_provided = settings is not None
    settings = settings or load_settings()
    validate_auth_settings()
    mcp_manager = (
        MCPServerManager() if execution_resolver is None and tool_registry is None else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_manager is not None:
            await mcp_manager.start()
        _log_available_internal_tools(app.state.execution_resolver)
        stale_recovery_task = asyncio.create_task(
            _recover_stale_runs_periodically(
                app.state.store,
                app.state.peer_agent_registry,
            )
        )
        try:
            yield
        finally:
            await _drain_active_runs(app)
            stale_recovery_task.cancel()
            try:
                await stale_recovery_task
            except asyncio.CancelledError:
                pass
            if mcp_manager is not None:
                await mcp_manager.stop()

    app = FastAPI(title="Minimal AI Agent Runtime", version="0.1.0", lifespan=lifespan)
    configure_tracing(app, settings.tracing)
    if thread_store is not None:
        app.state.store = thread_store
    elif settings_was_provided:
        app.state.store = build_thread_store(settings.thread_store)
    else:
        app.state.store = build_thread_store_from_env()
    app.state.store.recover_stale_runs()
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
    if execution_resolver is None:
        if llm_adapter is not None or tool_registry is not None:
            adapter = llm_adapter or build_llm_adapter_from_env()
            registry = tool_registry or build_tool_registry_from_env()
            execution_resolver = FixedTenantExecutionResolver(adapter, registry)
        else:
            config_source = resolve_tenant_config_source(tenant_config_source)
            fallback_resolver = build_execution_resolver_from_env(mcp_manager=mcp_manager)
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
                )
            else:
                raise RuntimeError(f"Unhandled tenant config source '{config_source}'")
    app.state.execution_resolver = execution_resolver
    app.state.quality_enhancer = QualityEnhancer()
    runtime_settings = settings.runtime
    app.state.image_input_settings = settings.image_input
    app.state.runtime_settings = runtime_settings
    app.state.runtime = AgentRuntime(
        store=app.state.store,
        execution_resolver=execution_resolver,
        max_iterations=runtime_settings.max_iterations,
        tool_timeout_seconds=runtime_settings.tool_timeout_seconds,
        quality_enhancer=app.state.quality_enhancer,
        context_compaction_enabled=runtime_settings.context_compaction_enabled,
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
    )
    app.include_router(build_admin_router())
    if WEB_CLIENT_DIR.exists():
        app.mount("/web", StaticFiles(directory=WEB_CLIENT_DIR, html=True), name="web")

    @app.get("/health")
    @app.get("/health/live")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=None)
    async def readiness() -> dict[str, object] | JSONResponse:
        checks = await database_readiness_checks()
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
        execution = request.app.state.execution_resolver.resolve(principal.tenant_id)
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        config = execution.config
        return ExecutionOptionsResponse(
            tenant_id=principal.tenant_id,
            skills=ExecutionOptionSection(
                default=config.skills.default_skill,
                items=[
                    ExecutionOptionItem(name=skill.name, description=skill.description)
                    for skill in config.skills.items
                ],
            ),
            capability_profiles=ExecutionOptionSection(
                default=config.capability_profiles.default_profile,
                items=[
                    ExecutionOptionItem(name=profile.name, description=profile.description)
                    for profile in config.capability_profiles.items
                ],
            ),
            llm_profiles=ExecutionOptionSection(
                default=config.default_llm_profile,
                items=[ExecutionOptionItem(name=name) for name in config.llm_profiles],
            ),
            agents=ExecutionAgentOptionSection(
                items=[
                    ExecutionAgentOptionItem(
                        name=agent.name,
                        description=agent.description,
                        skill_name=agent.skill_name,
                        skills=agent.skills,
                        capability_profile=agent.capability_profile,
                    )
                    for agent in config.agents.items
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

    @app.post("/threads", response_model=CreateThreadResponse)
    async def create_thread(
        request: Request,
        body: CreateThreadRequest | None = None,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> CreateThreadResponse:
        skill_name = body.skill_name if body is not None else None
        skill_names = body.skill_names if body is not None else None
        capability_profile = body.capability_profile if body is not None else None
        llm_profile = body.llm_profile if body is not None else None
        execution = request.app.state.execution_resolver.resolve(principal.tenant_id)
        enforce_thread_creation_limit(
            context=tenant_context_from_request_state(request.state),
            store=request.app.state.store,
        )
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        if skill_name is not None and skill_names is not None:
            raise HTTPException(
                status_code=400,
                detail="Provide either skill_name or skill_names, not both",
            )
        if skill_names is not None:
            duplicates = sorted({name for name in skill_names if skill_names.count(name) > 1})
            if duplicates:
                raise HTTPException(
                    status_code=400,
                    detail="Duplicate skill_names are not allowed: " + ", ".join(duplicates),
                )
            get_skill_configs(execution.config, skill_names)
        elif skill_name is not None:
            get_skill_config(execution.config, skill_name)
            skill_names = [skill_name]
        elif execution.config.skills.default_skill is not None:
            skill_names = [execution.config.skills.default_skill]
            skill_name = execution.config.skills.default_skill
        if capability_profile is not None:
            get_capability_profile(execution.config, capability_profile)
        if llm_profile is None:
            llm_profile = execution.config.default_llm_profile
        if llm_profile is not None and llm_profile not in execution.config.llm_profiles:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown LLM profile '{llm_profile}' for tenant '{principal.tenant_id}'",
            )
        thread = request.app.state.store.create_thread(
            principal.tenant_id,
            skill_name=skill_name,
            skill_names=skill_names,
            capability_profile=capability_profile,
            llm_profile=llm_profile,
        )
        return CreateThreadResponse(thread_id=thread.thread_id)

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
        request = _validate_and_normalize_message_request(
            request, app_request.app.state.image_input_settings
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
        execution = request.app.state.execution_resolver.resolve(principal.tenant_id)
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        enforce_thread_run_limit(
            context=tenant_context_from_request_state(request.state),
            store=request.app.state.store,
            thread_id=thread_id,
        )
        reply, _metadata = await _await_backend_run(request, principal, thread_id)
        return RunThreadResponse(reply=reply)

    @app.post("/threads/{thread_id}/run/stream", response_model=None)
    async def run_thread_stream(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> StreamingResponse:
        _reject_if_draining(request)
        execution = request.app.state.execution_resolver.resolve(principal.tenant_id)
        enforce_execution_entitlements(
            context=tenant_context_from_request_state(request.state),
            execution=execution,
        )
        enforce_thread_run_limit(
            context=tenant_context_from_request_state(request.state),
            store=request.app.state.store,
            thread_id=thread_id,
        )
        return StreamingResponse(
            _run_thread_ndjson_stream(request, principal, thread_id),
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
        request.app.state.store.get_thread(principal.tenant_id, thread_id)
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
        request.app.state.runtime.clear_private_values(principal, thread_id)

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
