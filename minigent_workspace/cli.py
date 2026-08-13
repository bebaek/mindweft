from __future__ import annotations

import argparse
import os
from pathlib import Path

from minigent_config.unified_config import DEFAULT_CODING_DOTENV_FILE


def parse_config_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coding workspace configuration commands.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    config_parser = subparsers.add_parser("config", help="Configuration helpers.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    export_parser = config_subparsers.add_parser(
        "export",
        help="Export a restartable local coding workspace config by merging API and runner config.",
    )
    env_file_group = export_parser.add_mutually_exclusive_group()
    env_file_group.add_argument(
        "--env-file",
        default=DEFAULT_CODING_DOTENV_FILE,
        help=f"Coding runner dotenv file. Defaults to {DEFAULT_CODING_DOTENV_FILE}.",
    )
    env_file_group.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load a coding runner dotenv file for this command.",
    )
    export_parser.add_argument(
        "--base-url",
        default=None,
        help="Running Minigent API URL. Defaults to MINIGENT_BASE_URL or http://127.0.0.1:8000.",
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write instead of stdout.",
    )
    export_parser.add_argument(
        "--include-runtime",
        action="store_true",
        help="Include informational runtime status/tool snapshots in the export.",
    )
    export_parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON instead of TOML.",
    )
    return parser.parse_args(argv)


def build_coding_config_export_client_argv(args: argparse.Namespace) -> list[str]:
    argv: list[str] = []
    base_url = args.base_url or os.getenv("MINIGENT_BASE_URL")
    if base_url:
        argv.extend(["--base-url", base_url])
    if args.json:
        argv.append("--json")
    argv.extend(["config", "export", "--local-coding"])
    if args.no_env_file:
        argv.append("--no-coding-env-file")
    else:
        argv.extend(["--coding-env-file", args.env_file])
    if args.output:
        argv.extend(["--output", args.output])
    if args.include_runtime:
        argv.append("--include-runtime")
    return argv


def load_config_command_env(env_file: str) -> None:
    from dotenv import dotenv_values

    path = Path(env_file).expanduser()
    os.environ["MINIGENT_DOTENV_FILE"] = str(path)
    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)


def run_config_command(argv: list[str]) -> int:
    args = parse_config_args(argv)
    if args.command == "config" and args.config_command == "export":
        if not args.no_env_file:
            load_config_command_env(args.env_file)
        from minigent_client.one_shot_cli import main as client_main

        return client_main(build_coding_config_export_client_argv(args))
    raise RuntimeError("unsupported coding workspace config command")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Minigent as a coding assistant with a workspace-scoped filesystem MCP server. "
            f"Loads {DEFAULT_CODING_DOTENV_FILE} by default."
        )
    )
    parser.add_argument(
        "--mcp-servers-file",
        default=None,
        help=(
            "Legacy JSON file declaring MCP servers to register/start. Prefer "
            "[[coding.mcp_server_specs]] in minigent.toml for new configs. Defaults to "
            "MINIGENT_CODING_MCP_SERVERS_FILE when set."
        ),
    )
    env_file_group = parser.add_mutually_exclusive_group()
    env_file_group.add_argument(
        "--env-file",
        default=DEFAULT_CODING_DOTENV_FILE,
        help=f"dotenv file to load before starting processes. Defaults to {DEFAULT_CODING_DOTENV_FILE}.",
    )
    env_file_group.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load a coding runner dotenv file before starting processes.",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=None,
        help=(
            "Workspace root to expose. Repeat for multiple roots. Defaults to "
            "MINIGENT_CODING_WORKSPACES, MINIGENT_CODING_WORKSPACE, or cwd."
        ),
    )
    parser.add_argument(
        "--workspace-scope",
        default=None,
        help=(
            "Named coding workspace scope to activate. Defaults to "
            "MINIGENT_CODING_WORKSPACE_SCOPE, a skill workspace_scope, "
            "MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE, or all configured workspaces."
        ),
    )
    parser.add_argument(
        "--mcp-gateway",
        action="store_true",
        help=(
            "Start stdio MCP servers behind one local gateway instead of one bridge process "
            "per server. Also rewrites generated tenant MCP URLs to /mcp/<server-name>."
        ),
    )
    parser.add_argument(
        "--mcp-gateway-port",
        type=int,
        default=None,
        help=(
            "Gateway port when --mcp-gateway is used. Defaults to "
            "MINIGENT_CODING_MCP_GATEWAY_PORT or the filesystem bridge port."
        ),
    )
    parser.add_argument(
        "--mcp-gateway-path-prefix",
        default=None,
        help=(
            "Gateway path prefix when --mcp-gateway is used. Defaults to "
            "MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX or /mcp."
        ),
    )
    parser.add_argument("--tenant-id", default=None, help="Tenant ID for generated default config.")
    parser.add_argument(
        "--api-host", default=None, help="API host. Defaults to MINIGENT_HOST or 127.0.0.1."
    )
    parser.add_argument(
        "--api-port", type=int, default=None, help="API port. Defaults to MINIGENT_PORT or 8000."
    )
    parser.add_argument(
        "--bridge-host",
        default=None,
        help="Filesystem MCP bridge host. Defaults to MINIGENT_CODING_BRIDGE_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=None,
        help="Filesystem MCP bridge port. Defaults to MINIGENT_CODING_BRIDGE_PORT or 8765.",
    )
    parser.add_argument(
        "--bridge-name",
        default=None,
        help="MCP server name. Defaults to MINIGENT_CODING_BRIDGE_NAME or fs-workspace.",
    )
    parser.add_argument(
        "--enable-shell",
        action="store_true",
        help="Also start a workspace-scoped shell MCP server and add a non-default test profile when generating config.",
    )
    parser.add_argument(
        "--shell-bridge-port",
        type=int,
        default=None,
        help="Shell MCP bridge port. Defaults to MINIGENT_CODING_SHELL_BRIDGE_PORT or 8766.",
    )
    parser.add_argument(
        "--shell-bridge-name",
        default=None,
        help="Shell MCP server name. Defaults to MINIGENT_CODING_SHELL_BRIDGE_NAME or shell-workspace.",
    )
    parser.add_argument(
        "--enable-text",
        action="store_true",
        help="Also start a workspace-scoped targeted text-read MCP server and add it to the inspect profile when generating config.",
    )
    parser.add_argument(
        "--text-bridge-port",
        type=int,
        default=None,
        help="Targeted text-read MCP bridge port. Defaults to MINIGENT_CODING_TEXT_BRIDGE_PORT or 8767.",
    )
    parser.add_argument(
        "--text-bridge-name",
        default=None,
        help="Targeted text-read MCP server name. Defaults to MINIGENT_CODING_TEXT_BRIDGE_NAME or text-workspace.",
    )
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="Only run the filesystem MCP bridge; do not start Minigent API.",
    )
    parser.add_argument(
        "--skip-bridge",
        action="store_true",
        help="Only run Minigent API; assumes the filesystem MCP bridge is already running.",
    )
    return parser.parse_args(argv)
