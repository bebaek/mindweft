from __future__ import annotations

import os
from pathlib import Path

from app.config import load_environment
from app.unified_config import apply_unified_config_to_env, load_unified_config_env


def test_unified_config_maps_common_desktop_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-from-env")
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
profile = "local-coding"

[app]
thread_db_path = ".data/minigent.db"
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
shell_enabled = true
shell_allowed_command_prefixes = ["uv ", "pytest ", "git "]

[mcp]
servers = [{ name = "filesystem", url = "http://127.0.0.1:8765/mcp", headers = {} }]

[quality]
enabled = false
""".strip(),
        encoding="utf-8",
    )

    env = load_unified_config_env(config_path)

    assert env["MINIGENT_THREAD_DB_PATH"] == ".data/minigent.db"
    assert env["MINIGENT_PORT"] == "9000"
    assert env["MINIGENT_AUTH_MODE"] == "development"
    assert env["MINIGENT_LLM_PROVIDER"] == "openrouter"
    assert env["MINIGENT_LLM_MODEL"] == "anthropic/claude-sonnet-4.5"
    assert env["OPENROUTER_MODEL"] == "anthropic/claude-sonnet-4.5"
    assert env["OPENROUTER_API_KEY"] == "secret-from-env"
    assert env["MINIGENT_CODING_MCP_GATEWAY_ENABLED"] == "true"
    assert env["MINIGENT_CODING_WORKSPACES"] == "/Users/example/code,/tmp/work"
    assert env["MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES"] == "uv ,pytest ,git "
    assert env["MINIGENT_MCP_SERVERS"] == (
        '[{"name":"filesystem","url":"http://127.0.0.1:8765/mcp","headers":{}}]'
    )
    assert env["MINIGENT_REMOTE_QUALITY_ENABLED"] == "false"


def test_load_environment_precedence_real_env_then_dotenv_then_minigent_toml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
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


def test_coding_runner_env_applies_minigent_toml_then_env_file_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
