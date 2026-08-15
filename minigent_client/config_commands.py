from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config_diagnostics import (  # noqa: F401 - preserve helper import surface.
    DiagnosticCheck,
    _coding_config_checks,
    _coding_mcp_timeout_checks,
    _config_bool,
    _env_bool_value,
    _env_snapshot_with_dotenv,
    _llm_config_checks,
    _local_config_checks,
    _mcp_config_checks,
    _named_llm_profile_checks,
    _positive_float_or_none,
    _unified_config_checks,
    _workspace_paths,
    collect_connection_checks,
    diagnostic_detail,
    format_check,
    package_version,
    run_config_doctor,
    server_config_checks,
    server_summary,
)
from minigent_client.config_export import (  # noqa: F401 - preserve helper import surface.
    _append_export_comment,
    _append_toml_array_table,
    _append_toml_table,
    _export_has_coding_gateway_mcp_urls,
    _export_llm_config,
    _export_mcp_server,
    _export_public_dict,
    _export_quality_config,
    _looks_like_local_coding_gateway_url,
    _merge_allowed_tools,
    _merge_export_details,
    _merge_split_mcp_server_spec,
    _prune_empty_values,
    _prune_export,
    _remove_tenant_mcp_server_entries,
    _string_list,
    _tenant_mcp_servers_by_name,
    _toml_inline_table,
    _toml_key,
    _toml_value,
    _unify_coding_mcp_server_config,
    export_unified_config_from_server,
    render_unified_config_toml,
)
from minigent_client.config_masking import (  # noqa: F401 - preserve helper import surface.
    mask_secrets,
    mask_value,
)
from minigent_client.output import print_json
from minigent_config.unified_config import (
    CONFIG_FILE_ENV,
    DEFAULT_DOTENV_FILE,
    DEFAULT_THREAD_DB_PATH,
    DEFAULT_VOICE_THREAD_DB_PATH,
    DOTENV_FILE_ENV,
    load_unified_config_env,
    resolve_config_path,
    resolve_dotenv_path,
)
from minigent_workspace.config_export import export_local_coding_config

DEFAULT_CONFIG_PROFILE = "local-coding"
CONFIG_INIT_PROFILES = ("basic-chat", "openrouter", "local-coding", "voice")

DEFAULT_CONFIG_TEMPLATE = f"""# Unified Minigent config facade.
# Keep secrets in your shell, OS keychain, or {DEFAULT_DOTENV_FILE}.
# Existing MINIGENT_* / provider env vars still override values from this file.

profile = "local-coding"

[app]
host = "127.0.0.1"
port = 8000

[auth]
mode = "development"

[llm]
provider = "mock"
# provider = "openrouter"
# model = "anthropic/claude-sonnet-4.5"
# api_key_env = "OPENROUTER_API_KEY"

[image_input]
# Enable attaching images with --image or /image when using a vision-capable model/provider.
enabled = false
# max_bytes = 5242880
# allowed_mime_types = ["image/png", "image/jpeg", "image/webp", "image/gif"]

[coding]
enabled = true
workspaces = ["/Users/you/code"]
shell_enabled = false
# shell_allowed_command_prefixes = ["uv ", "pytest ", "git "]
# default_workspace_scope = "my-app"
#
# [coding.workspace_scopes.my-app]
# roots = ["/Users/you/code/my-app"]
# description = "Primary app repository"

[quality]
enabled = false
"""

BASIC_CHAT_CONFIG_TEMPLATE = f"""# Basic local Minigent config.
# Uses the mock LLM provider so no API key is required.

profile = "basic-chat"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = "{DEFAULT_THREAD_DB_PATH}"

[auth]
mode = "development"

[llm]
provider = "mock"

[image_input]
# Enable attaching images with --image or /image when using a vision-capable model/provider.
enabled = false

[quality]
enabled = false
"""

OPENROUTER_CONFIG_TEMPLATE = f"""# Minigent config for OpenRouter-backed chat.
# Keep OPENROUTER_API_KEY in your environment or {DEFAULT_DOTENV_FILE}; do not commit it.

profile = "openrouter"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = "{DEFAULT_THREAD_DB_PATH}"

[auth]
mode = "development"

[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"

[image_input]
# Enable attaching images with --image or /image when using a vision-capable model/provider.
enabled = false

[quality]
enabled = false
"""

VOICE_CONFIG_TEMPLATE = f"""# Minigent voice-oriented config facade.
# Detailed audio/VAD tuning remains available through MINIGENT_VOICE_* env vars.

profile = "voice"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = "{DEFAULT_VOICE_THREAD_DB_PATH}"

[auth]
mode = "development"

[llm]
provider = "mock"
# provider = "openrouter"
# model = "anthropic/claude-sonnet-4.5"
# api_key_env = "OPENROUTER_API_KEY"

[image_input]
# Enable attaching images with --image or /image when using a vision-capable model/provider.
enabled = false

[voice]
tenant_id = "demo-tenant"
user_id = "voice-user"
skill = "assistant"
wake_phrase = "hey minigent"
stt_provider = "whisper"
tts_provider = "piper"

[quality]
enabled = false
"""

CONFIG_PROFILE_TEMPLATES = {
    "basic-chat": BASIC_CHAT_CONFIG_TEMPLATE,
    "openrouter": OPENROUTER_CONFIG_TEMPLATE,
    "local-coding": DEFAULT_CONFIG_TEMPLATE,
    "voice": VOICE_CONFIG_TEMPLATE,
}


def run_config(client: MinigentAPIClient, trace_id: str | None) -> int:
    response = client.config()
    if trace_id is not None:
        response = {**response, "trace_id": trace_id}
    print_json(response)
    return 0


def run_config_init(args: argparse.Namespace) -> int:
    output_path = Path(args.output).expanduser()
    if output_path.exists() and not args.force:
        raise RuntimeError(f"{output_path} already exists; use --force to overwrite")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile = getattr(args, "profile", DEFAULT_CONFIG_PROFILE)
    template = CONFIG_PROFILE_TEMPLATES[profile]
    output_path.write_text(template, encoding="utf-8")
    print(f"Wrote {output_path} ({profile})")
    return 0


def run_config_print(args: argparse.Namespace) -> int:
    output = collect_resolved_local_config()
    print_json(output)
    return 0


def run_config_export(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    server_config = client.config(export=True)
    export = export_unified_config_from_server(server_config)
    local_coding = getattr(args, "local_coding", False)
    if local_coding:
        _merge_export_details(export, export_local_coding_config(args))
        _unify_coding_mcp_server_config(export)
        export = _prune_export(export)
        export["profile"] = "exported-coding"
    elif _export_has_coding_gateway_mcp_urls(export):
        _append_export_comment(
            export,
            "This API-only export includes tenant MCP gateway URLs but not coding runner launch specs; "
            "use --local-coding or minigent-coding-workspace config export for a restartable coding stack.",
        )
    if not getattr(args, "include_runtime", False):
        export.pop("runtime", None)
    if trace_id is not None:
        export = {**export, "trace_id": trace_id}
    text = (
        json.dumps(export, indent=2, sort_keys=True) + "\n"
        if args.json
        else render_unified_config_toml(export)
    )
    output_path = getattr(args, "output", None)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
        if not args.json:
            print(f"Wrote {output_path}")
    else:
        print(text, end="")
    return 0


def collect_resolved_local_config() -> dict[str, object]:
    cwd = Path.cwd()
    env_snapshot = dict(os.environ)
    dotenv_path = resolve_dotenv_path(base_dir=cwd, env=env_snapshot)
    dotenv_keys: list[str] = []
    if dotenv_path is not None and dotenv_path.exists():
        from dotenv import dotenv_values

        dotenv = {
            key: value for key, value in dotenv_values(dotenv_path).items() if value is not None
        }
        dotenv_keys = sorted(dotenv)
        env_snapshot.update(dotenv)
    config_path = resolve_config_path(base_dir=cwd, env=env_snapshot)
    config_env = load_unified_config_env(config_path, source_env=env_snapshot)
    resolved = dict(config_env)
    for key in sorted(set(config_env) | set(env_snapshot)):
        if key in config_env or key.startswith(
            ("MINIGENT_", "OPENAI_", "OPENROUTER_", "GOOGLE_", "GEMINI_")
        ):
            if key in env_snapshot:
                resolved[key] = env_snapshot[key]
    return {
        "config_file": str(config_path) if config_path is not None else None,
        "config_file_env": os.environ.get(CONFIG_FILE_ENV),
        "dotenv_file_env": os.environ.get(DOTENV_FILE_ENV),
        "dotenv_file": str(dotenv_path)
        if dotenv_path is not None and dotenv_path.exists()
        else None,
        "dotenv_keys": dotenv_keys,
        "resolved_env": mask_secrets(dict(sorted(resolved.items()))),
    }
