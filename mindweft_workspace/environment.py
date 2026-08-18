from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from mindweft_client.state import state_dir_path
from mindweft_config.constants import ATTACHMENT_DB_PATH_ENV, THREAD_DB_PATH_ENV
from mindweft_config.unified_config import apply_unified_config_to_env, normalize_mindweft_env

DEFAULT_ATTACHMENT_DB_FILE = "attachments.db"
DEFAULT_THREAD_DB_FILE = "threads.db"


def apply_coding_workspace_state_defaults(env: dict[str, str]) -> None:
    """Use durable user-local API storage unless the deployment overrides it."""
    normalize_mindweft_env(env, warn_conflicts=True)
    state_dir = state_dir_path(env)
    env.setdefault(
        ATTACHMENT_DB_PATH_ENV,
        str(state_dir / DEFAULT_ATTACHMENT_DB_FILE),
    )
    env.setdefault(
        THREAD_DB_PATH_ENV,
        str(state_dir / DEFAULT_THREAD_DB_FILE),
    )


def load_env_file(env_file: str | None, *, warn_if_missing: bool = True) -> dict[str, str]:
    env = normalize_mindweft_env(dict(os.environ), warn_conflicts=True)
    if env_file is None:
        source_env = dict(env)
        apply_unified_config_to_env(source_env, base_dir=Path.cwd())
        for key, value in source_env.items():
            env.setdefault(key, value)
        apply_file_env_values(env, base_dir=Path.cwd())
        normalize_mindweft_env(env, warn_conflicts=True)
        return env

    path = Path(env_file)
    base_dir = path.parent if path.exists() else Path.cwd()
    values = dotenv_values(path) if path.exists() else {}
    source_env = dict(env)
    source_env.update({key: value for key, value in values.items() if value is not None})
    apply_unified_config_to_env(source_env, base_dir=base_dir)
    for key, value in source_env.items():
        env.setdefault(key, value)
    if path.exists():
        for key, value in values.items():
            if value is not None:
                env[key] = value
        apply_file_env_values(env, base_dir=path.parent)
        normalize_mindweft_env(env, warn_conflicts=True)
    else:
        if warn_if_missing:
            print(f"env file not found; continuing with current environment: {env_file}")
        apply_file_env_values(env, base_dir=Path.cwd())
        normalize_mindweft_env(env, warn_conflicts=True)
    return env


def apply_file_env_values(env: dict[str, str], *, base_dir: Path) -> None:
    """Expand FOO_FILE=/path/to/value-file entries into FOO=<file contents>.

    Relative file paths are resolved from the dotenv file directory. This is useful for
    long JSON-valued settings that are hard to edit safely on one dotenv line.
    """
    for file_key, raw_path in list(env.items()):
        if not file_key.endswith("_FILE") or not raw_path.strip():
            continue
        if file_key in {
            "MINIGENT_CONFIG_FILE",
            "MINIGENT_DOTENV_FILE",
            "MINIGENT_CODING_MCP_SERVERS_FILE",
            "MINDWEFT_CONFIG_FILE",
            "MINDWEFT_DOTENV_FILE",
            "MINDWEFT_CODING_MCP_SERVERS_FILE",
        }:
            continue
        target_key = file_key[: -len("_FILE")]
        value_path = Path(raw_path).expanduser()
        if not value_path.is_absolute():
            value_path = base_dir / value_path
        env[target_key] = value_path.read_text(encoding="utf-8").strip()
