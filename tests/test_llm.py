import httpx
import pytest
from fastapi import HTTPException

from app.llm import OpenAICompatibleAdapter, build_llm_adapter_from_env, load_provider_config
from app.models import Message, MessageRole
from app.tools import build_default_tool_registry


def test_openai_compatible_adapter_returns_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        payload = request.read().decode()
        assert '"model":"test-model"' in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello from provider"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = adapter.generate(
        [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
        build_default_tool_registry().specs(),
    )

    assert response.content == "hello from provider"
    assert response.tool_call is None


def test_openai_compatible_adapter_returns_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text":"hello from tool"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = adapter.generate(
        [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
        build_default_tool_registry().specs(),
    )

    assert response.content is None
    assert response.tool_call is not None
    assert response.tool_call.name == "echo"
    assert response.tool_call.arguments == {"text": "hello from tool"}


def test_load_provider_config_for_openrouter_includes_optional_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.com")
    monkeypatch.setenv("OPENROUTER_APP_NAME", "minigent")

    config = load_provider_config("openrouter")

    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.extra_headers == {
        "HTTP-Referer": "https://example.com",
        "X-OpenRouter-Title": "minigent",
    }


def test_build_llm_adapter_from_env_supports_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    adapter = build_llm_adapter_from_env()

    assert isinstance(adapter, OpenAICompatibleAdapter)


def test_build_llm_adapter_from_env_rejects_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_llm_adapter_from_env()


def test_openai_compatible_adapter_raises_for_invalid_tool_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "echo",
                                        "arguments": "{bad json",
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException, match="invalid tool arguments"):
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_default_tool_registry().specs(),
        )
