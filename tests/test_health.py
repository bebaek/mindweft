import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.health import database_readiness_checks
from app.llm import MockLLMAdapter
from app.main import _drain_active_runs, create_app
from app.tools import build_local_tool_registry


def test_database_readiness_checks_configured_sqlite_stores(tmp_path: Path) -> None:
    database = tmp_path / "shared.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ready (value INTEGER)")

    checks = asyncio.run(
        database_readiness_checks(
            {
                "MINIGENT_THREAD_DB_PATH": str(database),
                "MINIGENT_MCP_BROKER_DB_PATH": str(database),
                "MINIGENT_PRIVATE_VALUE_DB_PATH": str(database),
                "MINIGENT_PRIVATE_CONSENT_DB_PATH": str(database),
                "MINIGENT_ADMIN_DB_PATH": str(database),
                "MINIGENT_RATE_LIMIT_DB_PATH": str(database),
                "MINIGENT_OAUTH_STORE_PATH": str(database),
                "MINIGENT_OAUTH_ENCRYPTION_KEYS": '{"1":"configured"}',
            }
        )
    )

    assert checks == {
        "thread_store": True,
        "mcp_broker_store": True,
        "private_value_store": True,
        "private_consent_store": True,
        "admin_store": True,
        "rate_limit_store": True,
        "oauth_store": True,
    }


def test_database_readiness_checks_report_missing_database_without_disclosing_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.db"

    checks = asyncio.run(database_readiness_checks({"MINIGENT_THREAD_DB_PATH": str(missing)}))

    assert checks == {"thread_store": False}
    assert str(missing) not in repr(checks)


def test_health_live_remains_shallow_when_readiness_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
    )
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("MINIGENT_THREAD_DB_PATH", str(missing))

    with TestClient(app) as client:
        legacy = client.get("/health")
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert legacy.status_code == 200
    assert legacy.json() == {"status": "ok"}
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "checks": {"thread_store": "failed"},
    }
    assert str(missing) not in ready.text


def test_health_drain_is_loopback_only() -> None:
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
    )

    with TestClient(app) as client:
        response = client.post("/health/drain")

    assert response.status_code == 403
    assert response.json() == {"detail": "Drain endpoint is loopback-only"}


def test_health_drain_rejects_new_runs_and_marks_instance_unready() -> None:
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
    )

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        auth_headers = {
            "X-Minigent-User-Id": "user-1",
            "X-Minigent-Tenant-Id": "tenant-1",
        }
        thread_id = client.post("/threads", headers=auth_headers).json()["thread_id"]
        response = client.post("/health/drain")
        ready = client.get("/health/ready")
        run = client.post(
            f"/threads/{thread_id}/run",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json() == {"status": "draining", "cancelled_runs": 0}
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "checks": {"process": "ok", "lifecycle": "failed"},
    }
    assert run.status_code == 503
    assert run.json() == {"detail": "Instance is draining"}


def test_drain_cancels_and_waits_for_active_runs() -> None:
    async def scenario() -> None:
        app = create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def active_run() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(active_run())
        app.state.active_run_tasks[("tenant-1", "thread-1")] = task
        await started.wait()

        assert await _drain_active_runs(app) == 1
        assert cancelled.is_set()
        assert task.cancelled()
        assert app.state.accepting_runs is False

    asyncio.run(scenario())


def test_health_ready_succeeds_for_accessible_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
    )
    database = tmp_path / "threads.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ready (value INTEGER)")
    monkeypatch.setenv("MINIGENT_THREAD_DB_PATH", str(database))

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"thread_store": "ok"},
    }
