from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from app.admin_api import (
    admin_encryption_key_from_env,
    admin_store_path_from_env,
    build_admin_router,
)
from app.admin_store import SQLiteTenantConfigStore
from app.auth import require_principal, validate_auth_settings
from app.config import load_environment
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
from app.mcp_manager import MCPServerManager
from app.models import (
    AddMessageRequest,
    CreateThreadRequest,
    CreateThreadResponse,
    Message,
    MessageRole,
    Principal,
    RunThreadResponse,
)
from app.observability import configure_logging, configure_tracing
from app.redaction import install_log_redaction
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore
from app.tools import ToolRegistry, build_tool_registry_from_env

load_environment()
# Redact secrets in third-party logs like httpx request lines before any handler formats the record.
install_log_redaction()
configure_logging()

WEB_CLIENT_DIR = Path(__file__).resolve().parent / "static" / "web"


def create_app(
    llm_adapter: LLMAdapter | None = None,
    tool_registry: ToolRegistry | None = None,
    execution_resolver: TenantExecutionResolver | None = None,
    admin_store: SQLiteTenantConfigStore | None = None,
    tenant_config_source: str | None = None,
) -> FastAPI:
    validate_auth_settings()
    mcp_manager = MCPServerManager() if execution_resolver is None and tool_registry is None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _ = app
        if mcp_manager is not None:
            await mcp_manager.start()
        try:
            yield
        finally:
            if mcp_manager is not None:
                await mcp_manager.stop()

    app = FastAPI(title="Minimal AI Agent Runtime", version="0.1.0", lifespan=lifespan)
    configure_tracing(app)
    app.state.store = InMemoryThreadStore()
    app.state.mcp_manager = mcp_manager
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
    app.state.runtime = AgentRuntime(
        store=app.state.store,
        execution_resolver=execution_resolver,
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

    @app.post("/threads", response_model=CreateThreadResponse)
    async def create_thread(
        request: Request,
        body: CreateThreadRequest | None = None,
        principal: Principal = Depends(require_principal),
    ) -> CreateThreadResponse:
        skill_name = body.skill_name if body is not None else None
        skill_names = body.skill_names if body is not None else None
        capability_profile = body.capability_profile if body is not None else None
        execution = request.app.state.execution_resolver.resolve(principal.tenant_id)
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
        principal: Principal = Depends(require_principal),
    ) -> Message:
        return app_request.app.state.store.append_message(
            principal.tenant_id,
            Message(
                thread_id=thread_id,
                role=MessageRole.USER,
                content=request.content,
                created_by=principal.user_id,
            ),
        )

    @app.get("/threads/{thread_id}/messages", response_model=list[Message])
    async def get_messages(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_principal),
    ) -> list[Message]:
        return request.app.state.store.list_messages(principal.tenant_id, thread_id)

    @app.post("/threads/{thread_id}/run", response_model=RunThreadResponse)
    async def run_thread(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_principal),
    ) -> RunThreadResponse:
        return RunThreadResponse(
            reply=await request.app.state.runtime.run_thread(principal, thread_id)
        )

    @app.delete("/threads/{thread_id}", status_code=204)
    async def delete_thread(
        thread_id: str,
        request: Request,
        principal: Principal = Depends(require_principal),
    ) -> None:
        request.app.state.store.delete_thread(principal.tenant_id, thread_id)

    return app


app = create_app()
