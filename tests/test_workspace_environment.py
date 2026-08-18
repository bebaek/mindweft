from __future__ import annotations

from pathlib import Path

from minigent_workspace import environment


def test_coding_workspace_state_defaults_use_xdg_state_home(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}

    environment.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(tmp_path / "mindweft" / "attachments.db")
    assert env["MINIGENT_THREAD_DB_PATH"] == str(tmp_path / "mindweft" / "threads.db")


def test_coding_workspace_state_defaults_use_home_fallback(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}

    environment.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(
        tmp_path / ".local" / "state" / "mindweft" / "attachments.db"
    )
    assert env["MINIGENT_THREAD_DB_PATH"] == str(
        tmp_path / ".local" / "state" / "mindweft" / "threads.db"
    )


def test_coding_workspace_state_defaults_reuse_existing_minigent_state(tmp_path: Path) -> None:
    legacy_state = tmp_path / ".local" / "state" / "minigent"
    legacy_state.mkdir(parents=True)
    env = {"HOME": str(tmp_path)}

    environment.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(legacy_state / "attachments.db")
    assert env["MINIGENT_THREAD_DB_PATH"] == str(legacy_state / "threads.db")


def test_coding_workspace_state_defaults_accept_mindweft_overrides(tmp_path: Path) -> None:
    thread_path = tmp_path / "mindweft-threads.db"
    env = {
        "HOME": str(tmp_path),
        "MINDWEFT_THREAD_DB_PATH": str(thread_path),
    }

    environment.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_THREAD_DB_PATH"] == str(thread_path)
    assert env["MINDWEFT_THREAD_DB_PATH"] == str(thread_path)


def test_coding_workspace_state_defaults_preserve_storage_overrides(tmp_path: Path) -> None:
    configured_attachment_path = tmp_path / "custom-attachments.db"
    configured_thread_path = tmp_path / "custom-threads.db"
    env = {
        "HOME": str(tmp_path),
        "MINIGENT_ATTACHMENT_DB_PATH": str(configured_attachment_path),
        "MINIGENT_THREAD_DB_PATH": str(configured_thread_path),
    }

    environment.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(configured_attachment_path)
    assert env["MINIGENT_THREAD_DB_PATH"] == str(configured_thread_path)


def test_load_env_file_reads_file_backed_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_TENANT_EXECUTION_CONFIGS", raising=False)
    config_path = tmp_path / "tenant-config.json"
    config_path.write_text('{"demo-tenant":{"llm":{"provider":"mock"}}}\n', encoding="utf-8")
    env_path = tmp_path / ".env.coding"
    env_path.write_text(
        "MINIGENT_TENANT_EXECUTION_CONFIGS_FILE=tenant-config.json\n", encoding="utf-8"
    )

    env = environment.load_env_file(str(env_path))

    assert env["MINIGENT_TENANT_EXECUTION_CONFIGS"] == (
        '{"demo-tenant":{"llm":{"provider":"mock"}}}'
    )


def test_load_env_file_can_skip_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CODING_WORKSPACES", raising=False)
    env_path = tmp_path / ".env.coding"
    env_path.write_text("MINIGENT_CODING_WORKSPACES=/should/not/read\n", encoding="utf-8")

    env = environment.load_env_file(None)

    assert env.get("MINIGENT_CODING_WORKSPACES") != "/should/not/read"


def test_load_env_file_can_suppress_missing_default_message(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    environment.load_env_file(".env.coding", warn_if_missing=False)

    assert "env file not found" not in capsys.readouterr().out


def test_load_env_file_warns_for_explicit_missing_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    environment.load_env_file("missing.env", warn_if_missing=True)

    assert (
        "env file not found; continuing with current environment: missing.env"
        in capsys.readouterr().out
    )
