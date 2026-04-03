from __future__ import annotations

from fastapi import HTTPException

from app.llm import LLMAdapter, serialize_tool_result
from app.models import LLMResponse, Message, MessageRole, ThreadStatus
from app.store import InMemoryThreadStore
from app.tools import ToolRegistry


class AgentRuntime:
    def __init__(
        self,
        store: InMemoryThreadStore,
        llm_adapter: LLMAdapter,
        tool_registry: ToolRegistry,
        max_iterations: int = 8,
    ) -> None:
        self._store = store
        self._llm_adapter = llm_adapter
        self._tool_registry = tool_registry
        self._max_iterations = max_iterations

    def run_thread(self, thread_id: str) -> str:
        self._store.set_thread_status(thread_id, ThreadStatus.RUNNING)
        try:
            for _ in range(self._max_iterations):
                messages = self._store.list_messages(thread_id)
                response = self._llm_adapter.generate(messages, self._tool_registry.specs())
                if response.tool_call is not None:
                    self._handle_tool_call(thread_id, response)
                    continue

                if response.content is None:
                    raise HTTPException(status_code=500, detail="LLM returned neither content nor tool call")

                self._store.append_message(
                    Message(thread_id=thread_id, role=MessageRole.ASSISTANT, content=response.content)
                )
                self._store.set_thread_status(thread_id, ThreadStatus.IDLE)
                return response.content
        except HTTPException:
            self._store.set_thread_status(thread_id, ThreadStatus.ERROR)
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._store.set_thread_status(thread_id, ThreadStatus.ERROR)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        self._store.set_thread_status(thread_id, ThreadStatus.ERROR)
        raise HTTPException(status_code=500, detail="Agent exceeded maximum tool iterations")

    def _handle_tool_call(self, thread_id: str, response: LLMResponse) -> None:
        tool_call = response.tool_call
        if tool_call is None:
            return
        result = self._tool_registry.execute(tool_call.name, tool_call.arguments)
        self._store.append_message(
            Message(
                thread_id=thread_id,
                role=MessageRole.TOOL,
                content=serialize_tool_result(result),
                tool_name=tool_call.name,
            )
        )
