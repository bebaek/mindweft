import pytest


@pytest.fixture(autouse=True)
def isolate_thread_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep app tests independent from a developer's persisted thread DB setting."""
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)
