from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from minigent_workspace import scopes


def _scope_env(roots: dict[str, Path], **extra: str) -> dict[str, str]:
    return {
        "MINIGENT_CODING_WORKSPACE_SCOPES": json.dumps(
            {name: {"roots": [str(root)]} for name, root in roots.items()}
        ),
        **extra,
    }


def test_resolve_workspace_selection_uses_cli_roots_and_scope(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    selected = configured / "selected"
    selected.mkdir(parents=True)
    env = _scope_env({"selected": selected})

    roots, scope = scopes.resolve_workspace_selection(
        [str(configured)],
        "selected",
        env,
        tenant_id="demo-tenant",
    )

    assert roots == [selected.resolve()]
    assert scope is not None
    assert scope.name == "selected"


def test_resolve_workspace_selection_prefers_plural_environment_root(tmp_path: Path) -> None:
    plural = tmp_path / "plural"
    singular = tmp_path / "singular"
    plural.mkdir()
    singular.mkdir()
    env = {
        "MINIGENT_CODING_WORKSPACES": str(plural),
        "MINIGENT_CODING_WORKSPACE": str(singular),
    }

    roots, scope = scopes.resolve_workspace_selection(None, None, env, tenant_id="demo-tenant")

    assert roots == [plural.resolve()]
    assert scope is None


def test_resolve_workspace_selection_rejects_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="Workspace does not exist or is not a directory"):
        scopes.resolve_workspace_selection(
            [str(missing)],
            None,
            {},
            tenant_id="demo-tenant",
        )


def test_resolve_workspace_selection_rejects_scope_outside_explicit_roots(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "configured"
    outside = tmp_path / "outside"
    configured.mkdir()
    outside.mkdir()
    env = _scope_env({"outside": outside})

    with pytest.raises(RuntimeError, match="contains roots outside configured workspaces"):
        scopes.resolve_workspace_selection(
            [str(configured)],
            "outside",
            env,
            tenant_id="demo-tenant",
        )


def test_resolve_workspace_roots_splits_comma_separated_env_value(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"

    assert scopes.resolve_workspace_roots(None, f"{one},{two}") == [one.resolve(), two.resolve()]


def test_resolve_workspace_roots_splits_path_separator_env_value(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"

    assert scopes.resolve_workspace_roots(None, f"{one}{os.pathsep}{two}") == [
        one.resolve(),
        two.resolve(),
    ]


def test_resolve_workspace_roots_prefers_cli_values(tmp_path: Path) -> None:
    cli_root = tmp_path / "cli"
    env_root = tmp_path / "env"

    assert scopes.resolve_workspace_roots([str(cli_root)], str(env_root)) == [cli_root.resolve()]


def test_resolve_workspace_roots_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert scopes.resolve_workspace_roots(None, None) == [tmp_path.resolve()]


def test_load_workspace_scopes_preserves_description(tmp_path: Path) -> None:
    env = {
        "MINIGENT_CODING_WORKSPACE_SCOPES": json.dumps(
            {"repo": {"roots": [str(tmp_path)], "description": "Repo work"}}
        )
    }

    loaded = scopes.load_workspace_scopes_from_env(env)

    assert loaded["repo"] == scopes.WorkspaceScope(
        name="repo", roots=[tmp_path.resolve()], description="Repo work"
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{", "MINIGENT_CODING_WORKSPACE_SCOPES must be valid JSON"),
        ("[]", "MINIGENT_CODING_WORKSPACE_SCOPES must be a JSON object"),
        ('{"repo":{"roots":[]}}', "roots must be a non-empty string array"),
        ('{"repo":{"roots":["/tmp"],"description":3}}', "description must be a string"),
    ],
)
def test_load_workspace_scopes_rejects_invalid_config(raw: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        scopes.load_workspace_scopes_from_env({"MINIGENT_CODING_WORKSPACE_SCOPES": raw})


def test_resolve_active_workspace_scope_without_selection_preserves_roots(tmp_path: Path) -> None:
    roots = [tmp_path]

    resolved_roots, scope = scopes.resolve_active_workspace_scope(
        roots, {}, tenant_id="demo-tenant"
    )

    assert resolved_roots == roots
    assert scope is None


def test_resolve_active_workspace_scope_uses_default_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    env = {
        "MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE": "repo",
        "MINIGENT_CODING_WORKSPACE_SCOPES": json.dumps(
            {"repo": {"roots": [str(repo)], "description": "Repo work"}}
        ),
    }

    resolved_roots, scope = scopes.resolve_active_workspace_scope(
        [tmp_path], env, tenant_id="demo-tenant", validate_under_configured_roots=True
    )

    assert resolved_roots == [repo.resolve()]
    assert scope == scopes.WorkspaceScope(
        name="repo", roots=[repo.resolve()], description="Repo work"
    )


def test_resolve_active_workspace_scope_explicit_overrides_environment_scope(
    tmp_path: Path,
) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    env = _scope_env(
        {"one": one, "two": two},
        MINIGENT_CODING_WORKSPACE_SCOPE="one",
        MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE="one",
    )

    resolved_roots, scope = scopes.resolve_active_workspace_scope(
        [tmp_path], env, tenant_id="demo-tenant", explicit_scope="two"
    )

    assert resolved_roots == [two.resolve()]
    assert scope is not None
    assert scope.name == "two"


def test_resolve_active_workspace_scope_environment_overrides_skill_and_default(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    skill = tmp_path / "skill"
    fallback = tmp_path / "fallback"
    env = _scope_env(
        {"active": active, "skill": skill, "fallback": fallback},
        MINIGENT_CODING_WORKSPACE_SCOPE="active",
        MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE="fallback",
        MINIGENT_TENANT_EXECUTION_CONFIGS=json.dumps(
            {
                "demo-tenant": {
                    "skills": {
                        "default_skill": "coding",
                        "items": [{"name": "coding", "workspace_scope": "skill"}],
                    }
                }
            }
        ),
    )

    resolved_roots, scope = scopes.resolve_active_workspace_scope(
        [tmp_path], env, tenant_id="demo-tenant"
    )

    assert resolved_roots == [active.resolve()]
    assert scope is not None
    assert scope.name == "active"


def test_resolve_active_workspace_scope_uses_camel_case_skill_scope_before_default(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    fallback = tmp_path / "fallback"
    env = _scope_env(
        {"skill": skill, "fallback": fallback},
        MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE="fallback",
        MINIGENT_TENANT_EXECUTION_CONFIGS=json.dumps(
            {
                "demo-tenant": {
                    "skills": {
                        "defaultSkill": "coding",
                        "items": [{"name": "coding", "workspaceScope": "skill"}],
                    }
                }
            }
        ),
    )

    resolved_roots, scope = scopes.resolve_active_workspace_scope(
        [tmp_path], env, tenant_id="demo-tenant"
    )

    assert resolved_roots == [skill.resolve()]
    assert scope is not None
    assert scope.name == "skill"


def test_resolve_active_workspace_scope_requested_without_configured_scopes_fails(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="no workspace scopes are configured"):
        scopes.resolve_active_workspace_scope(
            [tmp_path],
            {"MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE": "missing"},
            tenant_id="demo-tenant",
        )


def test_resolve_active_workspace_scope_unknown_fails_with_available_scopes(
    tmp_path: Path,
) -> None:
    env = _scope_env({"known": tmp_path}, MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE="missing")

    with pytest.raises(
        RuntimeError,
        match="unknown coding workspace scope 'missing'. Available scopes: known",
    ):
        scopes.resolve_active_workspace_scope([tmp_path], env, tenant_id="demo-tenant")


def test_resolve_active_workspace_scope_rejects_roots_outside_configured_workspace(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "configured"
    outside = tmp_path / "outside"
    env = _scope_env({"outside": outside}, MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE="outside")

    with pytest.raises(RuntimeError, match="contains roots outside configured workspaces"):
        scopes.resolve_active_workspace_scope(
            [configured],
            env,
            tenant_id="demo-tenant",
            validate_under_configured_roots=True,
        )
