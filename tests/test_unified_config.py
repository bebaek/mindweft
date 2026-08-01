from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import load_environment
from app.execution import build_execution_resolver_from_env
from app.settings import load_settings
from app.unified_config import (
    apply_unified_config_to_env,
    load_unified_config_env,
    resolve_unified_config,
)


def test_unified_config_maps_common_desktop_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-from-env")
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
profile = "local-coding"

[app]
thread_db_path = ".data/minigent-threads.db"
port = 9000

[auth]
mode = "development"

[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"

[image_input]
enabled = true
max_bytes = 123456
allowed_mime_types = ["image/png", "image/webp"]

[coding]
enabled = true
workspaces = ["/Users/example/code", "/tmp/work"]
default_workspace_scope = "minigent"
shell_enabled = true
mcp_gateway_enabled = true
mcp_gateway_port = 9876
mcp_gateway_path_prefix = "/tools"
shell_allowed_command_prefixes = ["uv ", "pytest ", "git "]
mcp_server_specs = [{ name = "custom", transport = "stdio", command = ["custom-mcp"] }]

[coding.workspace_scopes.minigent]
roots = ["/Users/example/code/minigent"]
description = "Minigent repo"

[mcp]
servers = [{ name = "filesystem", url = "http://127.0.0.1:8765/mcp", headers = {} }]

[quality]
enabled = false
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path)

    assert env["MINIGENT_THREAD_DB_PATH"] == ".data/minigent-threads.db"
    assert env["MINIGENT_PORT"] == "9000"
    assert env["MINIGENT_AUTH_MODE"] == "development"
    assert env["MINIGENT_LLM_PROVIDER"] == "openrouter"
    assert env["MINIGENT_LLM_MODEL"] == "anthropic/claude-sonnet-4.5"
    assert env["OPENROUTER_MODEL"] == "anthropic/claude-sonnet-4.5"
    assert env["OPENROUTER_API_KEY"] == "secret-from-env"
    assert env["MINIGENT_IMAGE_INPUT_ENABLED"] == "true"
    assert env["MINIGENT_IMAGE_INPUT_MAX_BYTES"] == "123456"
    assert env["MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES"] == "image/png,image/webp"
    assert env["MINIGENT_CODING_MCP_GATEWAY_ENABLED"] == "true"
    assert env["MINIGENT_CODING_MCP_GATEWAY_PORT"] == "9876"
    assert env["MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX"] == "/tools"
    assert env["MINIGENT_CODING_WORKSPACES"] == "/Users/example/code,/tmp/work"
    assert env["MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE"] == "minigent"
    assert env["MINIGENT_CODING_WORKSPACE_SCOPES"] == (
        '{"minigent":{"roots":["/Users/example/code/minigent"],"description":"Minigent repo"}}'
    )
    assert env["MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES"] == "uv ,pytest ,git "
    assert env["MINIGENT_CODING_MCP_SERVER_SPECS"] == (
        '[{"name":"custom","transport":"stdio","command":["custom-mcp"]}]'
    )
    assert env["MINIGENT_MCP_SERVERS"] == (
        '[{"name":"filesystem","url":"http://127.0.0.1:8765/mcp","headers":{}}]'
    )
    assert env["MINIGENT_REMOTE_QUALITY_ENABLED"] == "false"


def test_centralized_settings_loader_uses_resolved_env_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-secret")
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[app]
thread_db_path = "threads.db"
max_iterations = 7
tool_timeout_seconds = 12.5
context_compaction_enabled = true

[llm]
provider = "openrouter"
model = "openai/gpt-test"
input_modalities = ["text", "image"]
api_key_env = "OPENROUTER_API_KEY"
base_url = "https://example.com/openrouter/v1"
extra_headers = { "X-Test" = "yes" }
account_id_header = "X-Account-ID"

[image_input]
enabled = true
max_bytes = 1234
max_images = 3
max_total_bytes = 2468
allowed_mime_types = ["image/png", "image/webp"]

[mcp]
servers = [{ name = "filesystem", url = "http://127.0.0.1:8765/mcp", headers = {} }]

[[peer_agents]]
name = "local-agent"
base_url = "http://127.0.0.1:9000"
description = "Local peer"

[logging]
level = "debug"
format = "json"

[quality]
enabled = true
provider = "openai-compatible"
model = "quality-model"
base_url = "https://example.com/quality/v1"
api_key = "quality-secret"
mode = "critique_draft"
timeout = 12.5
max_payload_chars = 4096
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path)
    env.update(
        {
            "MINIGENT_AGENT_BACKEND": "peer_agent",
            "MINIGENT_AGENT_BACKEND_PEER": "local-agent",
            "MINIGENT_AGENT_BACKEND_CWD": "/tmp/work",
            "MINIGENT_AGENT_BACKEND_TIMEOUT_SECONDS": "42",
            "MINIGENT_AGENT_BACKEND_POLL_INTERVAL_SECONDS": "0.5",
            "MINIGENT_MCP_BROKER_ENABLED": "false",
            "MINIGENT_AUTH_MODE": "static-tokens",
            "MINIGENT_AUTH_TOKENS": json.dumps(
                {
                    "token-1": {
                        "user_id": "user-1",
                        "tenant_id": "tenant-1",
                        "is_admin": True,
                    }
                }
            ),
            "MINIGENT_TENANT_CONFIG_SOURCE": "store-with-defaults",
            "MINIGENT_LLM_PROMPT_CACHE_KEY": "thread",
            "MINIGENT_LLM_REASONING_EFFORT": "low",
            "MINIGENT_LLM_REASONING_SUMMARY": "off",
            "MINIGENT_RESPONSES_REASONING_ONLY_RETRIES": "2",
            "MINIGENT_LLM_MAX_TOOL_RESULT_CHARS": "4096",
            "MINIGENT_LLM_DEBUG_LOG_RESPONSES": "true",
            "MINIGENT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS": "1234",
            "MINIGENT_LLM_DEBUG_REQUEST_LOG_PATH": "/tmp/llm-requests.jsonl",
            "MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH": "/tmp/llm-responses.jsonl",
            "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
                {
                    "tenant-1": {
                        "llm": {"provider": "mock"},
                        "tools": {"allowed_local_tools": ["calculator"]},
                    }
                }
            ),
        }
    )
    settings = load_settings(env)

    assert settings.thread_store.db_path == "threads.db"
    assert settings.runtime.max_iterations == 7
    assert settings.runtime.tool_timeout_seconds == 12.5
    assert settings.runtime.context_compaction_enabled is True
    assert settings.llm.provider == "openrouter"
    assert settings.llm.openrouter.model == "openai/gpt-test"
    assert settings.llm.openrouter.base_url == "https://example.com/openrouter/v1"
    assert settings.llm.openrouter.api_key == "router-secret"
    assert settings.llm.runtime.account_id_header == "X-Account-ID"
    assert settings.llm.runtime.prompt_cache_key == "thread"
    assert settings.llm.runtime.reasoning_effort == "low"
    assert settings.llm.runtime.reasoning_summary == "off"
    assert settings.llm.runtime.responses_reasoning_only_retries == 2
    assert settings.llm.runtime.max_tool_result_chars == 4096
    assert settings.llm.runtime.debug_log_responses is True
    assert settings.llm.runtime.debug_log_response_max_chars == 1234
    assert settings.llm.runtime.debug_request_log_path == "/tmp/llm-requests.jsonl"
    assert settings.llm.runtime.debug_response_log_path == "/tmp/llm-responses.jsonl"
    assert settings.agent_backend.type == "peer_agent"
    assert settings.agent_backend.peer == "local-agent"
    assert settings.agent_backend.cwd == "/tmp/work"
    assert settings.agent_backend.timeout_seconds == 42
    assert settings.agent_backend.poll_interval_seconds == 0.5
    assert settings.agent_backend.mcp_broker_enabled is False
    assert settings.auth.mode == "static-tokens"
    assert settings.auth.static_tokens["token-1"].user_id == "user-1"
    assert settings.auth.static_tokens["token-1"].tenant_id == "tenant-1"
    assert settings.auth.static_tokens["token-1"].is_admin is True
    assert settings.tenant_execution.config_source == "store-with-defaults"
    assert settings.tenant_execution.tenant_configs is not None
    assert settings.tenant_execution.tenant_configs["tenant-1"]["tools"] == {
        "allowed_local_tools": ["calculator"]
    }
    assert settings.tenant_execution.default_llm.provider == "openrouter"
    assert settings.tenant_execution.default_llm.model == "openai/gpt-test"
    assert settings.tenant_execution.default_llm.input_modalities == frozenset({"text", "image"})
    assert settings.quality.enabled is True
    assert settings.quality.provider == "openai-compatible"
    assert settings.quality.model == "quality-model"
    assert settings.quality.base_url == "https://example.com/quality/v1"
    assert settings.quality.api_key == "quality-secret"
    assert settings.quality.mode == "critique_draft"
    assert settings.quality.timeout == 12.5
    assert settings.quality.max_payload_chars == 4096
    assert settings.image_input.enabled is True
    assert settings.image_input.max_bytes == 1234
    assert settings.image_input.max_images == 3
    assert settings.image_input.max_total_bytes == 2468
    assert settings.image_input.allowed_mime_types == frozenset({"image/png", "image/webp"})
    assert settings.mcp.servers[0].name == "filesystem"
    assert settings.peer_agents.agents[0].name == "local-agent"
    assert settings.logging.level == "DEBUG"
    assert settings.logging.output_format == "json"


def test_unified_config_maps_anthropic_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_ANTHROPIC_KEY", "anthropic-secret")
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
provider = "anthropic"
model = "claude-test"
base_url = "https://example.com/anthropic/v1"
api_key_env = "MY_ANTHROPIC_KEY"
max_tokens = 8192
anthropic_version = "2023-06-01"
thinking_enabled = true
thinking_budget_tokens = 1024
thinking_effort = "medium"
prompt_cache_enabled = false
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path)

    assert env["MINIGENT_LLM_PROVIDER"] == "anthropic"
    assert env["MINIGENT_LLM_MODEL"] == "claude-test"
    assert env["ANTHROPIC_MODEL"] == "claude-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://example.com/anthropic/v1"
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert env["ANTHROPIC_MAX_TOKENS"] == "8192"
    assert env["ANTHROPIC_VERSION"] == "2023-06-01"
    assert env["ANTHROPIC_THINKING_ENABLED"] == "true"
    assert env["ANTHROPIC_THINKING_BUDGET_TOKENS"] == "1024"
    assert env["ANTHROPIC_THINKING_EFFORT"] == "medium"
    assert env["ANTHROPIC_PROMPT_CACHE_ENABLED"] == "false"


def test_unified_anthropic_profile_preserves_adaptive_thinking_options(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
default = "opus"

[llm.providers.opus]
provider = "anthropic"
model = "claude-opus-4-8"
api_key_env = "TEST_ANTHROPIC_KEY"
max_tokens = 8192
thinking_enabled = true
thinking_budget_tokens = 2048
thinking_effort = "medium"
prompt_cache_enabled = false
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(
        config_path,
        source_env={"TEST_ANTHROPIC_KEY": "secret"},
    )
    profiles = json.loads(env["MINIGENT_LLM_PROFILES"])
    assert profiles["opus"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "max_tokens": 8192,
        "thinking_enabled": True,
        "thinking_budget_tokens": 2048,
        "thinking_effort": "medium",
        "prompt_cache_enabled": False,
        "api_key": "${TEST_ANTHROPIC_KEY}",
    }

    context = build_execution_resolver_from_env(
        env={**env, "TEST_ANTHROPIC_KEY": "secret"}
    ).resolve("demo-tenant")
    adapter_description = context.llm_adapters["opus"].describe()
    assert adapter_description["model"] == "claude-opus-4-8"
    assert adapter_description["max_tokens"] == 8192
    assert adapter_description["thinking_mode"] == "adaptive"
    assert adapter_description["thinking_effort"] == "medium"
    assert adapter_description["prompt_cache_enabled"] is False


def test_unified_config_projects_top_level_llm_into_tenant_configs(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "MY_OPENROUTER_KEY"
base_url = "https://example.com/openrouter/v1"
extra_headers = { "X-Test" = "yes" }

[tenant_execution_configs.demo-tenant.tools]
allowed_local_tools = ["calculator"]
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path, source_env={"MY_OPENROUTER_KEY": "secret"})
    tenant_configs = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])

    assert env["OPENROUTER_API_KEY"] == "secret"
    assert tenant_configs["demo-tenant"]["llm"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-4.5",
        "base_url": "https://example.com/openrouter/v1",
        "extra_headers": {"X-Test": "yes"},
        "api_key": "${OPENROUTER_API_KEY}",
    }


def test_unified_config_preserves_explicit_tenant_llm_override(tmp_path: Path) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
provider = "openrouter"
model = "remote-model"
api_key_env = "OPENROUTER_API_KEY"

[tenant_execution_configs.demo-tenant.llm]
provider = "mock"

[tenant_execution_configs.demo-tenant.tools]
allowed_local_tools = ["calculator"]
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path, source_env={"OPENROUTER_API_KEY": "secret"})
    tenant_configs = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])

    assert tenant_configs["demo-tenant"]["llm"] == {"provider": "mock"}


def test_unified_config_projected_tenant_llm_beats_stale_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"

[tenant_execution_configs.demo-tenant.tools]
allowed_local_tools = ["calculator"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "mock")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    env = load_unified_config_env(config_path, source_env=dict(os.environ))
    runtime_env = {"MINIGENT_LLM_PROVIDER": "mock", "OPENROUTER_API_KEY": "secret"}
    for key, value in env.items():
        runtime_env.setdefault(key, value)

    context = build_execution_resolver_from_env(env=runtime_env).resolve("demo-tenant")

    assert context.config.llm.provider == "openrouter"
    assert context.config.llm.model == "anthropic/claude-sonnet-4.5"
    assert context.config.llm.api_key == "secret"
    assert context.llm_adapter.describe()["provider"] == "openrouter"


def test_unified_config_projects_coding_mcp_specs_into_tenant_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[coding]
mcp_gateway_enabled = true
mcp_gateway_port = 9876
mcp_gateway_path_prefix = "/tools"
mcp_server_specs = [
  { name = "fs-workspace", transport = "stdio", command = ["fs-mcp"], allowed_tools = ["read_file"] },
  { name = "web-fetch", url = "http://127.0.0.1:9001/mcp", allowed_tools = ["fetch"] },
]

[tenant_execution_configs.demo-tenant.tools]
allowed_local_tools = ["calculator"]

[tenant_execution_configs.demo-tenant.capability_profiles]
default_profile = "inspect"
items = [
  { name = "inspect", mcp_server_names = ["fs-workspace", "web-fetch"] },
]
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path)
    tenant_configs = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])

    servers = tenant_configs["demo-tenant"]["tools"]["mcp_servers"]
    assert servers == [
        {
            "name": "fs-workspace",
            "url": "http://127.0.0.1:9876/tools/fs-workspace",
            "allowed_tools": ["read_file"],
        },
        {
            "name": "web-fetch",
            "url": "http://127.0.0.1:9001/mcp",
            "allowed_tools": ["fetch"],
        },
    ]


def test_unified_config_imports_agent_skills_into_tenant_configs(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "code-reviewer"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code changes.\n"
        "allowed-tools: Read Grep\n"
        "---\n\n"
        "Review code carefully.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[agent_skills]
dirs = ["./skills"]

[tenant_execution_configs.demo-tenant.llm]
provider = "mock"

[tenant_execution_configs.demo-tenant.skills]
default_skill = "native"
items = [
  { name = "native", system_prompt = "Native prompt." },
]
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path)
    tenant_configs = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])

    items = tenant_configs["demo-tenant"]["skills"]["items"]
    assert items == [
        {"name": "native", "system_prompt": "Native prompt."},
        {
            "name": "code-reviewer",
            "description": "Reviews code changes.",
            "instruction_source": {
                "type": "agent_skill",
                "path": str(skill_md),
            },
        },
    ]


def test_unified_config_rejects_agent_skill_name_conflicts(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "code-reviewer"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: code-reviewer\ndescription: Reviews code changes.\n---\n\nReview.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[agent_skills]
dirs = ["./skills"]

[tenant_execution_configs.demo-tenant.skills]
items = [
  { name = "code-reviewer", system_prompt = "Native prompt." },
]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="conflict"):
        load_unified_config_env(config_path)


def test_load_environment_precedence_real_env_then_dotenv_then_minigent_toml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CONFIG_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
provider = "openrouter"
model = "from-toml"

[app]
thread_db_path = "from-toml.db"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "MINIGENT_THREAD_DB_PATH=from-dotenv.db\nOPENROUTER_MODEL=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "mock")
    monkeypatch.delenv("MINIGENT_LLM_MODEL", raising=False)
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    load_environment()

    assert os.environ["MINIGENT_LLM_PROVIDER"] == "mock"
    assert os.environ["MINIGENT_THREAD_DB_PATH"] == "from-dotenv.db"
    assert os.environ["OPENROUTER_MODEL"] == "from-dotenv"
    assert os.environ["MINIGENT_LLM_MODEL"] == "from-toml"


def test_load_environment_supports_custom_dotenv_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CONFIG_FILE", raising=False)
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "openrouter"
model = "from-toml"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("MINIGENT_LLM_PROVIDER=default-dotenv\n", encoding="utf-8")
    custom_dotenv = tmp_path / "custom.env"
    custom_dotenv.write_text(
        "MINIGENT_LLM_PROVIDER=mock\nMINIGENT_THREAD_DB_PATH=from-custom-dotenv.db\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIGENT_DOTENV_FILE", str(custom_dotenv))
    monkeypatch.delenv("MINIGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_MODEL", raising=False)
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)

    load_environment()

    assert os.environ["MINIGENT_LLM_PROVIDER"] == "mock"
    assert os.environ["MINIGENT_LLM_MODEL"] == "from-toml"
    assert os.environ["MINIGENT_THREAD_DB_PATH"] == "from-custom-dotenv.db"


def test_load_environment_can_disable_default_config_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CONFIG_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_MODEL", raising=False)
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "openrouter"
model = "from-toml"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("MINIGENT_THREAD_DB_PATH=from-dotenv.db\n", encoding="utf-8")

    load_environment(discover_default_files=False)

    assert "MINIGENT_LLM_PROVIDER" not in os.environ
    assert "MINIGENT_LLM_MODEL" not in os.environ
    assert "MINIGENT_THREAD_DB_PATH" not in os.environ


def test_resolve_unified_config_uses_xdg_user_config_when_cwd_config_is_absent(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    xdg_home = tmp_path / "xdg"
    user_config = xdg_home / "minigent" / "minigent.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('[llm]\nprovider = "mock"\n', encoding="utf-8")

    resolved = resolve_unified_config(
        base_dir=base_dir,
        env={"XDG_CONFIG_HOME": str(xdg_home)},
    )

    assert resolved.config_path == user_config
    assert resolved.env["MINIGENT_LLM_PROVIDER"] == "mock"


def test_resolve_unified_config_uses_home_config_without_xdg_override(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    home = tmp_path / "home"
    user_config = home / ".config" / "minigent" / "minigent.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('[llm]\nprovider = "mock"\n', encoding="utf-8")

    resolved = resolve_unified_config(base_dir=base_dir, env={"HOME": str(home)})

    assert resolved.config_path == user_config
    assert resolved.env["MINIGENT_LLM_PROVIDER"] == "mock"


def test_resolve_unified_config_prefers_cwd_config_over_user_config(tmp_path: Path) -> None:
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    local_config = base_dir / "minigent.toml"
    local_config.write_text('[llm]\nprovider = "mock"\n', encoding="utf-8")
    xdg_home = tmp_path / "xdg"
    user_config = xdg_home / "minigent" / "minigent.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "user-model"\n',
        encoding="utf-8",
    )

    resolved = resolve_unified_config(
        base_dir=base_dir,
        env={"XDG_CONFIG_HOME": str(xdg_home)},
    )

    assert resolved.config_path == local_config
    assert resolved.env["MINIGENT_LLM_PROVIDER"] == "mock"
    assert "MINIGENT_LLM_MODEL" not in resolved.env


def test_resolve_unified_config_honors_explicit_files_when_default_discovery_disabled(
    tmp_path: Path,
) -> None:
    default_config = tmp_path / "minigent.toml"
    default_config.write_text(
        """
[llm]
provider = "openrouter"
model = "default"
""".strip(),
        encoding="utf-8",
    )
    explicit_config = tmp_path / "explicit.toml"
    explicit_config.write_text(
        """
[llm]
provider = "mock"
""".strip(),
        encoding="utf-8",
    )
    explicit_dotenv = tmp_path / "explicit.env"
    explicit_dotenv.write_text("MINIGENT_THREAD_DB_PATH=explicit.db\n", encoding="utf-8")

    user_config = tmp_path / "home" / ".config" / "minigent" / "minigent.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('[llm]\nprovider = "openrouter"\nmodel = "user"\n', encoding="utf-8")

    resolved = resolve_unified_config(
        base_dir=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "MINIGENT_CONFIG_FILE": str(explicit_config),
            "MINIGENT_DOTENV_FILE": str(explicit_dotenv),
        },
        discover_default_files=False,
    )

    assert resolved.config_path == explicit_config
    assert resolved.dotenv_path == explicit_dotenv
    assert resolved.env["MINIGENT_LLM_PROVIDER"] == "mock"
    assert resolved.env["MINIGENT_THREAD_DB_PATH"] == "explicit.db"
    assert resolved.env.get("MINIGENT_LLM_MODEL") != "default"


def test_resolve_unified_config_env_can_disable_default_discovery(tmp_path: Path) -> None:
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "openrouter"
model = "default"
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_unified_config(
        base_dir=tmp_path,
        env={"MINIGENT_CONFIG_DISCOVERY": "disabled"},
    )

    assert resolved.config_path is None
    assert resolved.dotenv_path is None
    assert resolved.env == {}


def test_coding_runner_env_applies_minigent_toml_then_env_file_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CONFIG_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_CODING_WORKSPACES", raising=False)
    monkeypatch.delenv("MINIGENT_CODING_SHELL_ENABLED", raising=False)
    (tmp_path / "minigent.toml").write_text(
        """
[coding]
workspaces = ["/from/toml"]
shell_enabled = true
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.coding"
    env_file.write_text("MINIGENT_CODING_WORKSPACES=/from/dotenv\n", encoding="utf-8")

    env = dict(os.environ)
    apply_unified_config_to_env(env, base_dir=tmp_path)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        env[key] = value

    assert env["MINIGENT_CODING_WORKSPACES"] == "/from/dotenv"
    assert env["MINIGENT_CODING_SHELL_ENABLED"] == "true"


def test_unified_config_rejects_unknown_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
provider = "mock"
unknown = "value"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown key: llm.unknown"):
        load_unified_config_env(config_path)


def test_unified_config_rejects_wrong_value_types(tmp_path: Path) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[app]
port = "not-a-number"

[image_input]
enabled = "yes"
max_bytes = "big"
allowed_mime_types = ["image/png", 123]

[coding]
workspaces = ["/tmp", 123]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_unified_config_env(config_path)

    message = str(exc_info.value)
    assert "app.port must be an integer" in message
    assert "image_input.enabled must be a boolean" in message
    assert "image_input.max_bytes must be an integer" in message
    assert "image_input.allowed_mime_types must be a string or list of strings" in message
    assert "coding.workspaces must be a string or list of strings" in message


def test_unified_config_supports_named_llm_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm]
default = "primary"

[llm.providers.primary]
provider = "openai"
model = "gpt-test"
api_key_env = "PRIMARY_KEY"

[llm.providers.backup]
provider = "openai-compatible"
model = "local-test"
base_url = "http://localhost:11434/v1"
api_key_env = "BACKUP_KEY"
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(
        config_path,
        source_env={"PRIMARY_KEY": "primary-secret", "BACKUP_KEY": "backup-secret"},
    )

    assert env["MINIGENT_LLM_DEFAULT_PROFILE"] == "primary"
    assert env["MINIGENT_LLM_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "primary-secret"
    profiles = json.loads(env["MINIGENT_LLM_PROFILES"])
    assert profiles == {
        "primary": {
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "${PRIMARY_KEY}",
        },
        "backup": {
            "provider": "openai-compatible",
            "model": "local-test",
            "base_url": "http://localhost:11434/v1",
            "api_key": "${BACKUP_KEY}",
        },
    }


def test_unified_config_requires_default_for_multiple_llm_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[llm.providers.one]
provider = "mock"

[llm.providers.two]
provider = "mock"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="llm.default is required"):
        load_unified_config_env(config_path)
