from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.llm import LLMAdapter, MockLLMAdapter
from app.main import create_app
from app.models import LLMResponse, Message, MessageRole, ToolCall
from app.tools import build_local_tool_registry

AUTH_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}

OTHER_TENANT_HEADERS = {
    "X-Minigent-User-Id": "user-2",
    "X-Minigent-Tenant-Id": "tenant-2",
}


def test_thread_lifecycle_endpoints() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    config_response = client.get("/config")
    assert config_response.status_code == 200
    assert config_response.json()["llm"]["provider"] == "mock"

    create_response = client.post("/threads", headers=AUTH_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    add_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=AUTH_HEADERS,
    )
    assert add_response.status_code == 200
    assert add_response.json()["role"] == MessageRole.USER
    assert add_response.json()["created_by"] == "user-1"

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: hello"}

    messages_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]

    delete_response = client.delete(f"/threads/{thread_id}", headers=AUTH_HEADERS)
    assert delete_response.status_code == 204

    missing_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert missing_response.status_code == 404


def test_run_endpoint_handles_tool_call_flow() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from api"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": 'Tool result: {"echo": "hello from api"}'}

    messages = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS).json()
    assert [message["role"] for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]


def test_run_endpoint_returns_reply_when_tool_fails() -> None:
    class ToolFailingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            if messages and messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            return LLMResponse(
                tool_call=ToolCall(
                    id="call-fetch",
                    name="fetch_url",
                    arguments={"url": "https://example.com/missing"},
                )
            )

        def describe(self) -> dict[str, object]:
            return {
                "provider": "test",
                "model": None,
                "base_url": None,
                "headers": [],
                "adapter": "ToolFailingLLM",
            }

    class FailingRegistry:
        def specs(self) -> list[object]:
            return []

        def mcp_servers(self) -> list[dict[str, object]]:
            return []

        async def execute(self, name: str, arguments: dict[str, object]) -> object:
            raise HTTPException(status_code=502, detail="fetch_url failed with status 404")

    client = TestClient(create_app(llm_adapter=ToolFailingLLM(), tool_registry=FailingRegistry()))  # type: ignore[arg-type]
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "is austin airport open now"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {
        "reply": (
            'Tool result: {"error": {"tool_name": "fetch_url", "status_code": 502, '
            '"detail": "fetch_url failed with status 404"}}'
        )
    }


def test_thread_endpoints_require_authenticated_principal() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.post("/threads")

    assert response.status_code == 401
    assert "Missing authenticated principal" in response.json()["detail"]


def test_thread_endpoints_hide_cross_tenant_access() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    response = client.get(f"/threads/{thread_id}/messages", headers=OTHER_TENANT_HEADERS)

    assert response.status_code == 404
