import re
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


def test_production_dockerfile_includes_canonical_and_compatibility_packages() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY README.md LICENSE ./" in dockerfile
    for package in (
        "mindweft_archive",
        "mindweft_client",
        "mindweft_config",
        "mindweft_mcp",
        "mindweft_workspace",
        "minigent_client",
        "minigent_config",
        "minigent_mcp",
        "minigent_workspace",
    ):
        assert f"COPY {package} ./{package}" in dockerfile


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


def test_github_actions_are_pinned_to_full_commit_shas() -> None:
    action_pattern = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
    sha_pattern = re.compile(r"[0-9a-f]{40}")

    for workflow_path in sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = workflow_path.read_text(encoding="utf-8")
        for action in action_pattern.findall(workflow):
            if action.startswith("./"):
                continue
            _, separator, revision = action.rpartition("@")
            assert separator and sha_pattern.fullmatch(revision), (
                f"{workflow_path.relative_to(PROJECT_ROOT)} has an unpinned action: {action}"
            )


def test_release_workflow_stages_artifacts_before_publishing() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    build_index = workflow.index("name: Build and validate distributions")
    draft_index = workflow.index("name: Stage draft GitHub release")
    pypi_index = workflow.index("name: Publish distributions to PyPI")
    github_index = workflow.index("name: Publish immutable GitHub release")

    assert build_index < draft_index < pypi_index < github_index
    assert "environment:\n      name: pypi" in workflow
    assert "id-token: write" in workflow
    assert workflow.count("contents: write") == 2
    assert "--draft" in workflow
    assert "Verify staged GitHub release assets" in workflow
    assert "Check whether exact artifacts are already on PyPI" in workflow
    assert "Verify release immutability" in workflow
    assert "password:" not in workflow
