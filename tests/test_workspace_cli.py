from __future__ import annotations

from pathlib import Path

from app import coding_workspace_runner as legacy_runner
from minigent_workspace import cli


def test_runner_reexports_canonical_cli_helpers() -> None:
    names = [
        "parse_config_args",
        "build_coding_config_export_client_argv",
        "load_config_command_env",
        "run_config_command",
        "parse_args",
    ]

    for name in names:
        assert getattr(legacy_runner, name) is getattr(cli, name)


def test_coding_config_export_builds_client_argv(monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_BASE_URL", raising=False)
    args = cli.parse_config_args(
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

    assert cli.build_coding_config_export_client_argv(args) == [
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
    args = cli.parse_config_args(["config", "export", "--env-file", ".env.coding"])

    assert cli.build_coding_config_export_client_argv(args) == [
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
    args = cli.parse_config_args(["config", "export", "--no-env-file"])

    assert cli.build_coding_config_export_client_argv(args) == [
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

    cli.load_config_command_env(str(env_path))

    assert cli.os.environ["MINIGENT_DOTENV_FILE"] == str(env_path)
    assert cli.os.environ["MINIGENT_BASE_URL"] == "http://from-env.example"
    assert cli.os.environ["MINIGENT_CODING_TENANT_ID"] == "tenant"
