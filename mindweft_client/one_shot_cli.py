from __future__ import annotations

import sys
import urllib.parse
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.

from mindweft_client.admin_commands import (  # noqa: F401 - preserve handler import surface.
    run_admin_audit_list,
    run_admin_execution_config_export,
    run_admin_execution_config_import,
    run_admin_execution_config_validate_file,
    run_admin_tenant_entitlements,
    run_admin_tenant_users,
    run_admin_tenants_create,
    run_admin_tenants_list,
    run_admin_tenants_seed,
    run_admin_tenants_show,
    run_admin_tenants_transition,
    run_admin_tenants_update,
    run_admin_threads_delete,
    run_admin_threads_list,
    run_admin_threads_prune,
    run_admin_threads_show,
)
from mindweft_client.application import (  # noqa: F401 - preserve helper import surface.
    _abort_detail,
    _apply_cli_env_file,
    _print_abort_message,
    main,
)
from mindweft_client.chat_commands import (  # noqa: F401 - preserve helper import surface.
    _format_markdown_transcript,
    _read_run_message,
    build_client,
    build_config,
    build_trace_headers,
    ensure_thread,
    forget_thread,
    list_remembered_threads,
    load_remembered_thread,
    pick_thread_from_history,
    remember_thread,
    run_chat,
    run_export,
    run_import_thread_archive,
    run_resume,
    run_threads_create,
    run_threads_delete,
    run_threads_list,
    run_threads_retitle,
    run_threads_show,
    state_scope_key,
    validate_thread_create_options,
)
from mindweft_client.command_router import dispatch_command  # noqa: F401
from mindweft_client.config_commands import (  # noqa: F401 - preserve handler import surface.
    run_config,
    run_config_export,
    run_config_init,
    run_config_print,
)
from mindweft_client.config_diagnostics import (  # noqa: F401 - preserve handler import surface.
    run_config_doctor,
)
from mindweft_client.diagnostic_commands import (  # noqa: F401 - preserve helper surface.
    _client_config_summary,
    _debug_dict,
    _format_debug_bundle,
    _format_execution_agent_section,
    _format_execution_option_section,
    _format_execution_options,
    collect_debug_bundle,
    run_debug_bundle,
    run_execution_options,
    run_health,
    run_ping,
)
from mindweft_client.one_shot_parser import build_parser  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
