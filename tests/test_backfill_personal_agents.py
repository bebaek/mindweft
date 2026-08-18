import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

from app.admin_store import SQLiteTenantConfigStore

_MIGRATION_PATH = Path(__file__).parents[1] / "scripts" / "backfill_personal_agents.py"
_MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "backfill_personal_agents", _MIGRATION_PATH
)
assert _MIGRATION_SPEC is not None and _MIGRATION_SPEC.loader is not None
_MIGRATION_MODULE = importlib.util.module_from_spec(_MIGRATION_SPEC)
sys.modules["backfill_personal_agents"] = _MIGRATION_MODULE
_MIGRATION_SPEC.loader.exec_module(_MIGRATION_MODULE)
run = _MIGRATION_MODULE.run


def _args(db_path: Path, *, dry_run: bool) -> Namespace:
    return Namespace(
        db_path=str(db_path),
        encryption_key_env="TEST_ADMIN_ENCRYPTION_KEY",
        tenant_id=None,
        user_id=None,
        batch_size=100,
        max_conflicts=0,
        dry_run=dry_run,
    )


def test_backfill_personal_agents_is_idempotent_and_supports_dry_run(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "admin.db"
    store = SQLiteTenantConfigStore(str(db_path))
    store.upsert_user_execution_config("tenant-1", "user-1", {"skills": {"items": []}})

    assert run(_args(db_path, dry_run=True)) == 0
    dry_run_output = json.loads(capsys.readouterr().out)
    assert dry_run_output["scanned"] == 1
    assert dry_run_output["updated"] == 1
    assert "defaults" not in store.get_user_execution_config("tenant-1", "user-1").config

    assert run(_args(db_path, dry_run=False)) == 0
    apply_output = json.loads(capsys.readouterr().out)
    assert apply_output["updated"] == 1
    migrated = store.get_user_execution_config("tenant-1", "user-1")
    assert migrated is not None
    assert migrated.config["defaults"]["agent_ref"] == "user:personal-assistant"
    assert (
        migrated.config["agents"]["items"][0]["capability_profile_ref"]
        == "user:mindweft-user-tools"
    )
    assert migrated.config["capability_profiles"]["items"][0]["mcp_server_refs"] == [
        "shared:mindweft-user-mcp"
    ]

    assert run(_args(db_path, dry_run=False)) == 0
    rerun_output = json.loads(capsys.readouterr().out)
    assert rerun_output["updated"] == 0
    assert rerun_output["already_default"] == 1
