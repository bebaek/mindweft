from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from minigent_workspace import config_export


def test_export_local_coding_config_prefers_mindweft_environment(
    tmp_path: Path, monkeypatch
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    canonical.mkdir()
    legacy.mkdir()
    monkeypatch.setenv("MINDWEFT_CODING_WORKSPACES", str(canonical))
    monkeypatch.setenv("MINIGENT_CODING_WORKSPACES", str(legacy))
    monkeypatch.setenv("MINDWEFT_CODING_TENANT_ID", "canonical-tenant")
    monkeypatch.setenv("MINIGENT_CODING_TENANT_ID", "legacy-tenant")

    exported = config_export.export_local_coding_config(
        Namespace(no_coding_env_file=True, coding_env_file=None, env_file=None)
    )

    assert exported["coding"]["workspaces"] == [str(canonical)]
    assert exported["coding"]["tenant_id"] == "canonical-tenant"


def test_export_local_coding_config_reuses_preloaded_env_file(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env.coding"
    env_path.write_text("MINIGENT_CODING_WORKSPACES=/should/not/read\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.delenv("MINDWEFT_DOTENV_FILE", raising=False)
    monkeypatch.setenv("MINIGENT_DOTENV_FILE", str(env_path))
    monkeypatch.setenv("MINIGENT_CODING_WORKSPACES", str(workspace))
    monkeypatch.setenv("MINIGENT_CODING_TENANT_ID", "preloaded-tenant")

    def fail_load_env_file(_path: str) -> dict[str, str]:
        raise AssertionError("env file should not be read again")

    def fail_apply_file_env_values(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("global *_FILE scan should not run")

    monkeypatch.setattr(config_export.environment, "load_env_file", fail_load_env_file)
    monkeypatch.setattr(
        config_export.environment,
        "apply_file_env_values",
        fail_apply_file_env_values,
    )

    exported = config_export.export_local_coding_config(
        Namespace(coding_env_file=str(env_path), env_file=str(env_path))
    )

    assert exported["coding"]["workspaces"] == [str(workspace)]
    assert exported["coding"]["tenant_id"] == "preloaded-tenant"


def test_export_local_coding_config_can_skip_env_file(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env.coding"
    env_path.write_text("MINIGENT_CODING_WORKSPACES=/should/not/read\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINIGENT_CODING_WORKSPACES", str(workspace))
    monkeypatch.setenv("MINIGENT_CODING_TENANT_ID", "env-only-tenant")

    def fail_load_env_file(_path: str) -> dict[str, str]:
        raise AssertionError("env file should not be read")

    monkeypatch.setattr(config_export.environment, "load_env_file", fail_load_env_file)

    exported = config_export.export_local_coding_config(
        Namespace(no_coding_env_file=True, coding_env_file=str(env_path), env_file=str(env_path))
    )

    assert exported["coding"]["workspaces"] == [str(workspace)]
    assert exported["coding"]["tenant_id"] == "env-only-tenant"


def test_load_coding_workspace_export_env_applies_file_values_without_dotenv_reread(
    tmp_path: Path, monkeypatch
) -> None:
    env_path = tmp_path / ".env.coding"
    env_path.write_text("MINIGENT_CODING_WORKSPACES=/should/not/read\n", encoding="utf-8")
    tenant_config_path = tmp_path / "tenant.json"
    tenant_config_path.write_text('{"demo-tenant":{"llm":{"provider":"mock"}}}', encoding="utf-8")
    monkeypatch.delenv("MINDWEFT_DOTENV_FILE", raising=False)
    monkeypatch.setenv("MINIGENT_DOTENV_FILE", str(env_path))
    monkeypatch.setenv("MINIGENT_TENANT_EXECUTION_CONFIGS_FILE", "tenant.json")
    monkeypatch.setenv("UNRELATED_BLOCKING_FILE", "unrelated.fifo")

    def fail_apply_file_env_values(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("global *_FILE scan should not run")

    monkeypatch.setattr(
        config_export.environment,
        "apply_file_env_values",
        fail_apply_file_env_values,
    )

    env, base_dir = config_export.load_coding_workspace_export_env(env_path)

    assert base_dir == tmp_path
    assert env["MINIGENT_TENANT_EXECUTION_CONFIGS"] == '{"demo-tenant":{"llm":{"provider":"mock"}}}'
