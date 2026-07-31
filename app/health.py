from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

_READINESS_DATABASE_ENVS: tuple[tuple[str, str], ...] = (
    ("thread_store", "MINIGENT_THREAD_DB_PATH"),
    ("mcp_broker_store", "MINIGENT_MCP_BROKER_DB_PATH"),
    ("private_value_store", "MINIGENT_PRIVATE_VALUE_DB_PATH"),
    ("private_consent_store", "MINIGENT_PRIVATE_CONSENT_DB_PATH"),
    ("admin_store", "MINIGENT_ADMIN_DB_PATH"),
)
_OAUTH_STORE_PATH_ENV = "MINIGENT_OAUTH_STORE_PATH"
_OAUTH_ENCRYPTION_KEY_ENVS = (
    "MINIGENT_OAUTH_ENCRYPTION_KEY",
    "MINIGENT_OAUTH_ENCRYPTION_KEYS",
)


async def database_readiness_checks(
    env: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    lookup = os.environ if env is None else env
    configured = [
        (name, value)
        for name, variable in _READINESS_DATABASE_ENVS
        if (value := lookup.get(variable, "").strip())
    ]
    if any(lookup.get(variable, "").strip() for variable in _OAUTH_ENCRYPTION_KEY_ENVS):
        oauth_path = lookup.get(_OAUTH_STORE_PATH_ENV, "").strip()
        if oauth_path:
            configured.append(("oauth_store", oauth_path))

    if not configured:
        return {"process": True}

    results = await asyncio.gather(
        *(
            asyncio.to_thread(_sqlite_database_is_ready, database_path)
            for _, database_path in configured
        )
    )
    return {name: ready for (name, _), ready in zip(configured, results, strict=True)}


def _sqlite_database_is_ready(database_path: str) -> bool:
    path = Path(database_path).expanduser()
    uri = f"file:{quote(str(path), safe='/')}?mode=rw"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            connection.execute("PRAGMA busy_timeout = 500")
            connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False
    return True
