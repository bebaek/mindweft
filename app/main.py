from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.admin_api import (
    admin_encryption_key_from_env,
    admin_store_path_from_env,
    build_admin_router,
)
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
from app.llm import LLMAdapter, build_llm_adapter_from_env
from app.mcp_broker import MCPBrokerSessionStore, handle_mcp_broker_request
from app.mcp_manager import MCPServerManager
from app.models import (
    AddMessageRequest,
    CreateThreadRequest,
    CreateThreadResponse,
    ExecutionOptionItem,
    ExecutionOptionSection,
    ExecutionOptionsResponse,
    Message,
    MessageRole,
    Principal,
    RunThreadResponse,
    TenantContext,
    TextPart,
    ThreadStatus,
)
from app.oauth import GenericOAuthProvider, OAuthFlowStore
from app.observability import configure_logging, configure_tracing
from app.peer_agents import PeerAgentRegistry, build_peer_agent_registry_from_env
from app.quality import QualityEnhancer
from app.redaction import install_log_redaction
from app.runtime import (
    AgentRuntime,
    context_compaction_enabled_from_env,
    estimate_thread_context_usage,
    max_iterations_from_env,
    render_raw_thread_context,
    tool_timeout_seconds_from_env,
)
from app.store import ThreadStore, build_thread_store_from_env
from app.tenants import require_active_tenant_principal, require_tenant_context
from app.tools import ToolRegistry, build_tool_registry_from_env

load_environment()
# Redact secrets in third-party logs like httpx request lines before any handler formats the record.
install_log_redaction()
configure_logging()

logger = logging.getLogger(__name__)
WEB_CLIENT_DIR = Path(__file__).resolve().parent / "static" / "web"

IMAGE_INPUT_ENABLED_ENV = "MINIGENT_IMAGE_INPUT_ENABLED"
IMAGE_INPUT_MAX_BYTES_ENV = "MINIGENT_IMAGE_INPUT_MAX_BYTES"
IMAGE_INPUT_ALLOWED_MIME_TYPES_ENV = "MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES"
DEFAULT_IMAGE_INPUT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _allowed_image_mime_types() -> set[str]:
    configured = os.getenv(IMAGE_INPUT_ALLOWED_MIME_TYPES_ENV, "").strip()
    if not configured:
        return DEFAULT_IMAGE_INPUT_ALLOWED_MIME_TYPES
    return {item.strip().lower() for item in configured.split(",") if item.strip()}


def _max_image_input_bytes() -> int:
    configured = os.getenv(IMAGE_INPUT_MAX_BYTES_ENV, "").strip()
    if not configured:
        return DEFAULT_IMAGE_INPUT_MAX_BYTES
    try:
        return int(configured)
    except ValueError:
        return DEFAULT_IMAGE_INPUT_MAX_BYTES


def _validate_and_normalize_message_request(request: AddMessageRequest) -> AddMessageRequest:
    if not request.parts:
        if not request.content:
            raise HTTPException(status_code=400, detail="message content is required")
        return request
    has_image = any(part.type == "image" for part in request.parts)
    if has_image and not _env_bool(IMAGE_INPUT_ENABLED_ENV):
        raise HTTPException(status_code=400, detail="image input is disabled")
    allowed_mime_types = _allowed_image_mime_types()
    max_bytes = _max_image_input_bytes()
    for part in request.parts:
        if part.type != "image":
            continue
        mime_type = part.mime_type.lower()
        if mime_type not in allowed_mime_types:
            raise HTTPException(
                status_code=400, detail=f"unsupported image MIME type: {part.mime_type}"
            )
        if not part.data and not part.url and not part.attachment_id:
            raise HTTPException(
                status_code=400, detail="image part must include data, url, or attachment_id"
            )
        if part.data:
            try:
                decoded = base64.b64decode(part.data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=400, detail="image data must be base64") from exc
            if len(decoded) > max_bytes:
                raise HTTPException(status_code=400, detail="image exceeds maximum allowed size")
    if request.content:
        return request
    text_content = "\n".join(
        part.text for part in request.parts if isinstance(part, TextPart) and part.text
    )
    return request.model_copy(update={"content": text_content})


async def _run_thread_ndjson_stream(
    request: Request,
    principal: Principal,
    thread_id: str,
) -> AsyncIterator[str]:
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
    request.app.state.active_run_tasks[run_key] = task
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
) -> FastAPI:
    validate_auth_settings()
    mcp_manager = (
        MCPServerManager() if execution_resolver is None and tool_registry is None else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_manager is not None:
            await mcp_manager.start()
        _log_available_internal_tools(app.state.execution_resolver)
        try:
            yield
        finally:
            if mcp_manager is not None:
                await mcp_manager.stop()

    app = FastAPI(title="Minimal AI Agent Runtime", version="0.1.0", lifespan=lifespan)
    configure_tracing(app)
    app.state.store = thread_store or build_thread_store_from_env()
    app.state.mcp_manager = mcp_manager
    app.state.mcp_broker_sessions = MCPBrokerSessionStore()
    app.state.oauth_flows = OAuthFlowStore()
    admin_encryption_key = admin_encryption_key_from_env()
    if admin_store is None:
        admin_db_path = admin_store_path_from_env()
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
                if admin_encryption_key is None and admin_store_path_from_env() is not None:
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
                if admin_encryption_key is None and admin_store_path_from_env() is not None:
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
    app.state.runtime = AgentRuntime(
        store=app.state.store,
        execution_resolver=execution_resolver,
        max_iterations=max_iterations_from_env(),
        tool_timeout_seconds=tool_timeout_seconds_from_env(),
        quality_enhancer=app.state.quality_enhancer,
        context_compaction_enabled=context_compaction_enabled_from_env(),
    )
    app.state.peer_agent_registry = peer_agent_registry or build_peer_agent_registry_from_env()
    app.state.active_run_tasks = {}
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
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config")
    async def config(request: Request) -> dict[str, object]:
        return request.app.state.execution_resolver.describe()

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
        return await handle_mcp_broker_request(
            session_store=request.app.state.mcp_broker_sessions,
            session_id=session_id,
            request=request,
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

    @app.post("/threads", response_model=CreateThreadResponse)
    async def create_thread(
        request: Request,
        body: CreateThreadRequest | None = None,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> CreateThreadResponse:
        skill_name = body.skill_name if body is not None else None
        skill_names = body.skill_names if body is not None else None
        capability_profile = body.capability_profile if body is not None else None
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
        thread = request.app.state.store.create_thread(
            principal.tenant_id,
            skill_name=skill_name,
            skill_names=skill_names,
            capability_profile=capability_profile,
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
        request = _validate_and_normalize_message_request(request)
        return app_request.app.state.store.append_message(
            principal.tenant_id,
            Message(
                thread_id=thread_id,
                role=MessageRole.USER,
                content=request.content,
                parts=request.parts,
                created_by=principal.user_id,
                metadata=request.metadata,
            ),
        )

    @app.get("/threads/{thread_id}/messages", response_model=list[Message])
    async def get_messages(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> list[Message]:
        return request.app.state.store.list_messages(principal.tenant_id, thread_id)

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
        reply, _metadata = await request.app.state.agent_backend.run_thread(principal, thread_id)
        return RunThreadResponse(reply=reply)

    @app.post("/threads/{thread_id}/run/stream", response_model=None)
    async def run_thread_stream(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> StreamingResponse:
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
        task = request.app.state.active_run_tasks.pop(run_key, None)
        cancelled = False
        if task is not None and not task.done():
            task.cancel()
            cancelled = True
            try:
                await task
            except asyncio.CancelledError:
                pass
        thread = request.app.state.store.get_thread(principal.tenant_id, thread_id)
        if thread.status == ThreadStatus.RUNNING:
            request.app.state.store.set_thread_status(
                principal.tenant_id,
                thread_id,
                ThreadStatus.IDLE,
            )
        return {"cancelled": cancelled, "thread_id": thread_id}

    @app.delete("/threads/{thread_id}", status_code=204)
    async def delete_thread(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_active_tenant_principal),
    ) -> None:
        request.app.state.store.delete_thread(principal.tenant_id, thread_id)

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
