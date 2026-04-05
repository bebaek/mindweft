from __future__ import annotations

from fastapi import Depends, FastAPI, Request

from app.auth import require_principal
from app.config import load_environment
from app.llm import LLMAdapter, build_llm_adapter_from_env
from app.models import (
    AddMessageRequest,
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


def create_app(
    llm_adapter: LLMAdapter | None = None, tool_registry: ToolRegistry | None = None
) -> FastAPI:
    app = FastAPI(title="Minimal AI Agent Runtime", version="0.1.0")
    configure_tracing(app)
    app.state.store = InMemoryThreadStore()
    tool_registry = tool_registry or build_tool_registry_from_env()
    adapter = llm_adapter or build_llm_adapter_from_env()
    app.state.llm_adapter = adapter
    app.state.tool_registry = tool_registry
    app.state.runtime = AgentRuntime(
        store=app.state.store,
        llm_adapter=adapter,
        tool_registry=tool_registry,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config")
    async def config(request: Request) -> dict[str, object]:
        return {
            "llm": request.app.state.llm_adapter.describe(),
            "mcp_servers": request.app.state.tool_registry.mcp_servers(),
        }

    @app.post("/threads", response_model=CreateThreadResponse)
    async def create_thread(
        request: Request, principal: Principal = Depends(require_principal)
    ) -> CreateThreadResponse:
        thread = request.app.state.store.create_thread(principal.tenant_id)
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
