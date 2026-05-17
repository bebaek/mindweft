from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

RUN_INTEGRATION_ENV = "MINIGENT_RUN_LLAMA_CPP_INTEGRATION_TESTS"
pytestmark = pytest.mark.integration

pytestmark = pytest.mark.skipif(
    os.getenv(RUN_INTEGRATION_ENV, "").lower() not in {"1", "true", "yes", "on"},
    reason=f"Set {RUN_INTEGRATION_ENV}=true to run llama.cpp integration tests",
)


def load_demo_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "demo_local_quality.py"
    spec = importlib.util.spec_from_file_location("demo_local_quality", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_local_quality_runs_against_llama_cpp(capsys) -> None:
    demo = load_demo_module()
    exit_code = demo.main(
        [
            "--llama-base-url",
            os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080/v1"),
            "--llama-model",
            os.getenv("LLAMA_CPP_MODEL", "local-model"),
            "--quality-provider",
            "mock",
            "--message",
            "Give a two sentence answer about local-first remote quality review.",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "quality.sanitized" in output
    assert "quality.applied" in output
    assert "assistant:" in output
