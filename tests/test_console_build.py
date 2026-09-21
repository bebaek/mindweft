from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "_build_console.py"))
build_console = module["build_console"]


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    web = root / "web"
    web.mkdir(parents=True)
    for name in module["WEB_CONFIG"]:
        (web / name).write_text("build input")
    for folder in ("src", "e2e"):
        (web / folder).mkdir()
        (web / folder / "example.ts").write_text("export {};")
    # Synthetic fixtures, not developer configuration or real secrets.
    (web / "ignored-local-config").write_text("do not copy")
    (web / "dist").mkdir()
    (web / "dist" / "stale.js").write_text("stale")
    build_lib = tmp_path / "build" / "lib"
    target = build_lib / "app" / "static" / "console"
    target.mkdir(parents=True)
    (target / "stale.js").write_text("old staged bundle")
    monkeypatch.setattr(module["shutil"], "which", lambda name: "/usr/bin/npm")

    def npm(command, *, cwd, check):
        assert check
        assert cwd != web
        assert not (cwd / "ignored-local-config").exists()
        assert not (cwd / "dist" / "stale.js").exists()
        if command == ["npm", "run", "build"]:
            (cwd / "dist" / "assets").mkdir(parents=True)
            (cwd / "dist" / "index.html").write_text("fresh")
            (cwd / "dist" / "assets" / "fresh.js").write_text("fresh JS")

    run = Mock(side_effect=npm)
    monkeypatch.setattr(module["subprocess"], "run", run)
    return root, build_lib, target, run


def test_build_uses_clean_sources_and_replaces_old_assets(source):
    root, build_lib, target, run = source
    build_console(root, build_lib)
    assert run.call_count == 2
    assert "--include=dev" in run.call_args_list[0].args[0]
    assert (target / "index.html").read_text() == "fresh"
    assert not (target / "stale.js").exists()
    assert (root / "web" / "dist" / "stale.js").exists()
    assert not list(build_lib.parent.glob(".console-source-*"))


def test_missing_npm_fails_even_with_staged_assets(source, monkeypatch):
    root, build_lib, target, run = source
    monkeypatch.setattr(module["shutil"], "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Node.js/npm"):
        build_console(root, build_lib)
    run.assert_not_called()
    assert (target / "stale.js").exists()


def test_failed_npm_never_falls_back(source):
    root, build_lib, target, run = source
    run.side_effect = subprocess.CalledProcessError(1, "npm")
    with pytest.raises(RuntimeError, match="refusing to build"):
        build_console(root, build_lib)
    assert (target / "stale.js").exists()
    assert not list(build_lib.parent.glob(".console-source-*"))


def test_empty_build_fails(source):
    root, build_lib, _, run = source
    run.side_effect = None
    with pytest.raises(RuntimeError, match="no usable"):
        build_console(root, build_lib)


def test_missing_lockfile_fails(source):
    root, build_lib, _, run = source
    (root / "web" / "package-lock.json").unlink()
    with pytest.raises(RuntimeError, match="package-lock.json"):
        build_console(root, build_lib)
    run.assert_not_called()


def test_symlinked_source_fails(source):
    root, build_lib, _, run = source
    (root / "web" / "src" / "linked.ts").symlink_to(root / "web" / "src" / "example.ts")
    with pytest.raises(RuntimeError, match="Symlinked frontend"):
        build_console(root, build_lib)
    run.assert_not_called()
