from __future__ import annotations

from fastapi import FastAPI, Request

from app.llm import MockLLMAdapter
from app.models import AddMessageRequest, CreateThreadResponse, Message, MessageRole, RunThreadResponse
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore
from app.tools import build_default_tool_registry

def create_app() -> FastAPI:
    app = FastAPI(title="Minimal AI Agent Runtime", version="0.1.0")
    app.state.store = InMemoryThreadStore()
    app.state.runtime = AgentRuntime(
        store=app.state.store,
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_default_tool_registry(),
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/threads", response_model=CreateThreadResponse)
    def create_thread(request: Request) -> CreateThreadResponse:
        thread = request.app.state.store.create_thread()
        return CreateThreadResponse(thread_id=thread.thread_id)

    @app.post("/threads/{thread_id}/messages", response_model=Message)
    def add_message(thread_id: str, request: AddMessageRequest, app_request: Request) -> Message:
        return app_request.app.state.store.append_message(
            Message(thread_id=thread_id, role=MessageRole.USER, content=request.content)
        )

    @app.get("/threads/{thread_id}/messages", response_model=list[Message])
    def get_messages(thread_id: str, request: Request) -> list[Message]:
        return request.app.state.store.list_messages(thread_id)

    @app.post("/threads/{thread_id}/run", response_model=RunThreadResponse)
    def run_thread(thread_id: str, request: Request) -> RunThreadResponse:
        return RunThreadResponse(reply=request.app.state.runtime.run_thread(thread_id))

    @app.delete("/threads/{thread_id}", status_code=204)
    def delete_thread(thread_id: str, request: Request) -> None:
        request.app.state.store.delete_thread(thread_id)

    return app


app = create_app()
