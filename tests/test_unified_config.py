from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import load_environment
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
    assert env["ANTHROPIC_PROMPT_CACHE_ENABLED"] == "false"


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

    resolved = resolve_unified_config(
        base_dir=tmp_path,
        env={
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

[coding]
workspaces = ["/tmp", 123]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_unified_config_env(config_path)

    message = str(exc_info.value)
    assert "app.port must be an integer" in message
    assert "coding.workspaces must be a string or list of strings" in message
