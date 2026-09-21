from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "mindweft_dev_script", Path(__file__).resolve().parents[1] / "scripts" / "dev.py"
)
assert spec is not None and spec.loader is not None
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / "web" / "dist").mkdir(parents=True)
    (tmp_path / "web" / "dist" / "index.html").write_text("new console")
    target = tmp_path / "app" / "static" / "console"
    target.mkdir(parents=True)
    (target / "index.html").write_text("old console")
    (target / "stale.js").write_text("old bundle")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(dev.subprocess, "run", run)
    return tmp_path, target, run


def test_build_stages_fresh_assets_without_stale_bundles(checkout):
    root, target, run = checkout
    assert dev.main(["build"]) == 0
    assert (target / "index.html").read_text() == "new console"
    assert not (target / "stale.js").exists()
    assert [call.args[0][:2] for call in run.call_args_list] == [["npm", "ci"], ["npm", "run"]]
    assert all(call.kwargs["cwd"] == root for call in run.call_args_list)
    assert not list(target.parent.glob(".console-build-*"))


@pytest.mark.parametrize("command", ["code"])
def test_failed_build_preserves_packaged_assets_and_does_not_launch(checkout, command):
    _, target, run = checkout
    run.side_effect = subprocess.CalledProcessError(7, ["npm"])
    assert dev.main([command]) == 7
    assert run.call_count == 1
    assert (target / "index.html").read_text() == "old console"


def test_copy_failure_preserves_old_console(checkout, monkeypatch):
    _, target, _ = checkout
    monkeypatch.setattr(dev.shutil, "copytree", Mock(side_effect=OSError("copy failed")))
    assert dev.main(["build"]) == 1
    assert (target / "index.html").read_text() == "old console"


def test_failed_publication_restores_old_console(checkout, monkeypatch):
    _, target, _ = checkout
    rename = Path.rename

    def fail_new(self, destination):
        if self.name == "new":
            raise OSError("publication failed")
        return rename(self, destination)

    monkeypatch.setattr(Path, "rename", fail_new)
    assert dev.main(["build"]) == 1
    assert (target / "index.html").read_text() == "old console"


def test_code_preserves_caller_cwd_and_forwards_arguments(checkout):
    root, _, run = checkout
    assert dev.main(["code", "project with spaces", "--instance", "preview", "--demo"]) == 0
    assert run.call_args.args[0] == [
        "uv",
        "run",
        "--project",
        str(root),
        "mindweft",
        "code",
        "project with spaces",
        "--instance",
        "preview",
        "--demo",
    ]
    assert "cwd" not in run.call_args.kwargs


def test_code_accepts_leading_options(checkout):
    _, _, run = checkout
    assert dev.main(["code", "--instance", "preview"]) == 0
    assert run.call_args.args[0][-2:] == ["--instance", "preview"]


def test_install_uses_checkout_and_requested_python(checkout):
    root, _, run = checkout
    assert dev.main(["install", "--python", "3.13"]) == 0
    assert run.call_count == 1  # The wheel hook builds the console; no duplicate staging build.
    assert run.call_args.args[0] == [
        "uv",
        "tool",
        "install",
        "--reinstall",
        "--python",
        "3.13",
        str(root),
    ]


def test_launch_exit_code_is_preserved(checkout):
    _, _, run = checkout
    run.return_value = subprocess.CompletedProcess([], 9)
    assert dev.main(["code"]) == 9


def test_missing_dist_does_not_replace_console(checkout):
    root, target, _ = checkout
    (root / "web" / "dist" / "index.html").unlink()
    assert dev.main(["build"]) == 1
    assert (target / "index.html").read_text() == "old console"
