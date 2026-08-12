from __future__ import annotations

import importlib.metadata
from pathlib import Path

from app import coding_workspace_runner as runner


def test_console_script_entry_point_loads_runner_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == "minigent-coding-workspace")

    loaded = entry_point.load()
    assert loaded.__module__ == "app.coding_workspace_runner"
    assert loaded.__name__ == "main"


def test_coding_config_export_builds_client_argv(monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_BASE_URL", raising=False)
    args = runner.parse_config_args(
        [
            "config",
            "export",
            "--env-file",
            ".env.test",
            "--base-url",
            "http://127.0.0.1:9000",
            "--output",
            "export.toml",
            "--include-runtime",
        ]
    )

    assert runner.build_coding_config_export_client_argv(args) == [
        "--base-url",
        "http://127.0.0.1:9000",
        "config",
        "export",
        "--local-coding",
        "--coding-env-file",
        ".env.test",
        "--output",
        "export.toml",
        "--include-runtime",
    ]


def test_coding_config_export_uses_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_BASE_URL", "http://127.0.0.1:9100")
    args = runner.parse_config_args(["config", "export", "--env-file", ".env.coding"])

    assert runner.build_coding_config_export_client_argv(args) == [
        "--base-url",
        "http://127.0.0.1:9100",
        "config",
        "export",
        "--local-coding",
        "--coding-env-file",
        ".env.coding",
    ]


def test_coding_config_export_can_skip_env_file(monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_BASE_URL", raising=False)
    args = runner.parse_config_args(["config", "export", "--no-env-file"])

    assert runner.build_coding_config_export_client_argv(args) == [
        "config",
        "export",
        "--local-coding",
        "--no-coding-env-file",
    ]


def test_load_config_command_env_sets_dotenv_without_overriding(
    tmp_path: Path, monkeypatch
) -> None:
    env_path = tmp_path / ".env.coding"
    env_path.write_text(
        "MINIGENT_BASE_URL=http://from-dotenv.example\nMINIGENT_CODING_TENANT_ID=tenant\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIGENT_BASE_URL", "http://from-env.example")
    monkeypatch.delenv("MINIGENT_CODING_TENANT_ID", raising=False)

    runner.load_config_command_env(str(env_path))

    assert runner.os.environ["MINIGENT_DOTENV_FILE"] == str(env_path)
    assert runner.os.environ["MINIGENT_BASE_URL"] == "http://from-env.example"
    assert runner.os.environ["MINIGENT_CODING_TENANT_ID"] == "tenant"


def test_coding_workspace_state_defaults_use_xdg_state_home(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}

    runner.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(tmp_path / "minigent" / "attachments.db")


def test_coding_workspace_state_defaults_use_home_fallback(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}

    runner.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(
        tmp_path / ".local" / "state" / "minigent" / "attachments.db"
    )


def test_coding_workspace_state_defaults_preserve_attachment_override(tmp_path: Path) -> None:
    configured_path = tmp_path / "custom-attachments.db"
    env = {
        "HOME": str(tmp_path),
        "MINIGENT_ATTACHMENT_DB_PATH": str(configured_path),
    }

    runner.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(configured_path)


def test_load_env_file_reads_file_backed_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_TENANT_EXECUTION_CONFIGS", raising=False)
    config_path = tmp_path / "tenant-config.json"
    config_path.write_text('{"demo-tenant":{"llm":{"provider":"mock"}}}\n', encoding="utf-8")
    env_path = tmp_path / ".env.coding"
    env_path.write_text(
        "MINIGENT_TENANT_EXECUTION_CONFIGS_FILE=tenant-config.json\n", encoding="utf-8"
    )

    env = runner.load_env_file(str(env_path))

    assert env["MINIGENT_TENANT_EXECUTION_CONFIGS"] == (
        '{"demo-tenant":{"llm":{"provider":"mock"}}}'
    )


def test_load_env_file_can_skip_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CODING_WORKSPACES", raising=False)
    env_path = tmp_path / ".env.coding"
    env_path.write_text("MINIGENT_CODING_WORKSPACES=/should/not/read\n", encoding="utf-8")

    env = runner.load_env_file(None)

    assert env.get("MINIGENT_CODING_WORKSPACES") != "/should/not/read"


def test_load_env_file_can_suppress_missing_default_message(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    runner.load_env_file(".env.coding", warn_if_missing=False)

    assert "env file not found" not in capsys.readouterr().out


def test_load_env_file_warns_for_explicit_missing_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    runner.load_env_file("missing.env", warn_if_missing=True)

    assert (
        "env file not found; continuing with current environment: missing.env"
        in capsys.readouterr().out
    )
