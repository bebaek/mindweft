from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "template_name",
    [".env.template", ".env.coding.template", ".env.docker.template"],
)
def test_environment_templates_use_canonical_mindweft_namespace(template_name: str) -> None:
    lines = (PROJECT_ROOT / template_name).read_text(encoding="utf-8").splitlines()

    assert lines[0] == (
        "# Canonical Mindweft environment names; matching MINIGENT_* names remain supported."
    )
    assert any("MINDWEFT_" in line for line in lines[1:])
    assert all("MINIGENT_" not in line for line in lines[1:])


def test_compose_files_use_canonical_runtime_environment() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    peer_demo = (PROJECT_ROOT / "compose.peer-demo.yaml").read_text(encoding="utf-8")
    pi_demo = (PROJECT_ROOT / "compose.pi-backend-demo.yaml").read_text(encoding="utf-8")

    assert "${MINDWEFT_IMAGE:-${MINIGENT_IMAGE:-mindweft:latest}}" in compose
    assert "${MINDWEFT_ENV_FILE:-${MINIGENT_ENV_FILE:-.env}}" in compose
    assert "MINDWEFT_LOG_FORMAT: json" in compose
    for demo in (peer_demo, pi_demo):
        assert "MINDWEFT_AUTH_MODE: dev-headers" in demo
        assert "${MINDWEFT_PORT:-${MINIGENT_PORT:-8000}}" in demo
        assert "/workspace/mindweft" in demo


def test_shell_entrypoints_prefer_mindweft_with_legacy_fallbacks() -> None:
    demo = (PROJECT_ROOT / "scripts/demo_pi_backend_stack.sh").read_text(encoding="utf-8")
    runner = (PROJECT_ROOT / "scripts/run-client-linux.sh").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "scripts/install-client-linux.sh").read_text(encoding="utf-8")

    assert "${MINDWEFT_PORT:-${MINIGENT_PORT:-8000}}" in demo
    assert "MINDWEFT_AUTH_MODE=dev-headers" in demo
    assert "${MINDWEFT_VOICE_ENV_FILE:-${MINIGENT_VOICE_ENV_FILE:-.env.voice}}" in runner
    assert "exec mindweft-client" in runner
    assert 'SERVICE_PATH="$SERVICE_DIR/mindweft-client.service"' in installer
    assert 'LEGACY_CLIENT_SERVICE_PATH="$SERVICE_DIR/minigent-client.service"' in installer
    assert "Environment=MINDWEFT_VOICE_ENV_FILE=" in installer


def test_container_publish_scripts_default_to_mindweft_images() -> None:
    runtime_script = (PROJECT_ROOT / "scripts/docker-build-push.sh").read_text(encoding="utf-8")
    peer_script = (PROJECT_ROOT / "scripts/docker-build-push-pi-peer-agent.sh").read_text(
        encoding="utf-8"
    )

    assert 'IMAGE_NAME="${IMAGE_NAME:-mindweft}"' in runtime_script
    assert 'IMAGE_NAME="${IMAGE_NAME:-mindweft-local-agent-wrapper}"' in peer_script

    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "/mindweft-app" in workflow
    assert "mindweft-production-smoke" in workflow
    assert "scope=mindweft" in workflow
