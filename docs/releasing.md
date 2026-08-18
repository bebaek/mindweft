# Releasing Mindweft

This checklist covers distribution readiness. Publishing, repository renames, tags, and releases
are deliberate external operations and are not performed by the ordinary CI workflow.

## One-time prerequisites

Before the first public package release:

- Confirm ownership or availability of the `mindweft` project name on the target package index.
- Confirm the Apache-2.0 license remains appropriate and add a `NOTICE` file if required by any
  bundled attribution notices.
- Confirm maintainer metadata and add authors or maintainers to `pyproject.toml` if desired.
- Confirm the canonical source repository URLs in `pyproject.toml` resolve correctly.
- Configure package-index trusted publishing or another approved credential mechanism without
  committing tokens to the repository.
- Decide whether `0.1.0` is the intended first published version. Published versions cannot be
  replaced with different artifacts.

## Prepare a release

1. Start from a clean checkout of the intended release commit.
2. Move completed entries from `[Unreleased]` in `CHANGELOG.md` into a versioned section with the
   release date.
3. Update the version in `pyproject.toml` and run `uv lock`.
4. Run the standard checks:

   ```bash
   uv sync --locked --dev --extra voice
   uv run ruff check .
   uv run ruff format --check .
   uv run basedpyright
   uv run pytest
   ```

5. Build and validate both distribution formats:

   ```bash
   rm -rf dist
   uv build --out-dir dist
   python scripts/verify_distribution_artifacts.py dist --version <version>
   ```

6. Install the wheel into a new environment and run the installed-package smoke test:

   ```bash
   smoke_dir="$(mktemp -d)"
   uv venv "$smoke_dir/venv"
   uv pip install --python "$smoke_dir/venv/bin/python" dist/mindweft-*.whl
   (
     cd "$smoke_dir"
     "$smoke_dir/venv/bin/python" \
       "$OLDPWD/scripts/smoke_test_installed_distribution.py" --version <version>
   )
   rm -rf "$smoke_dir"
   ```

7. Inspect the rendered package description and metadata on a staging index when available.
8. Tag and publish only the exact artifacts that passed validation.
9. Verify the published wheel and source distribution by installing them in a clean environment.
10. Create release notes from `CHANGELOG.md` and include the migration guide when compatibility
    behavior changes.

## Automated package publishing

The `.github/workflows/release.yml` workflow publishes tags matching `v*` through PyPI Trusted
Publishing and GitHub immutable releases. Before using it:

1. Configure the `mindweft` project on PyPI with the GitHub publisher owner `bebaek`, repository
   `mindweft`, workflow `release.yml`, and environment `pypi`.
2. Configure the GitHub `pypi` environment to allow release tags such as `v*`; requiring a
   maintainer's approval is recommended.
3. Enable immutable releases in the repository settings and protect `v*` tags from updates and
   deletion.
4. Merge the release preparation change to `main` and wait for its CI checks to pass.
5. Create an annotated or signed tag whose version exactly matches `project.version`, then push it:

   ```bash
   git tag -a v0.1.0 -m "Mindweft 0.1.0"
   git push origin v0.1.0
   ```

The workflow rejects tags whose version differs from `pyproject.toml` or whose commit is not
reachable from `origin/main`. It then:

1. Builds, validates, and smoke-tests the distributions in an unprivileged job.
2. Creates a draft GitHub release from the matching `CHANGELOG.md` section and attaches the exact
   validated artifacts.
3. Verifies the draft assets by SHA-256 digest.
4. Publishes those artifacts to PyPI with a short-lived GitHub OIDC credential.
5. Publishes the fully populated GitHub draft, which makes the release and its assets immutable.

If PyPI already contains the version, a rerun proceeds only when every remote artifact digest
matches the validated files. Likewise, a rerun may refresh a draft release but refuses to modify a
published mutable release. No PyPI token should be stored in GitHub Secrets, and release artifacts
must never be rebuilt between these stages.

## Publication safety

- Never rebuild artifacts after approval; publish the already-validated files from `dist/`.
- Never reuse an existing version number.
- Never place package-index tokens in command history, tracked files, workflow logs, or environment
  templates.
- Keep legacy import and command smoke tests until their removal is covered by an announced
  deprecation policy.
- Do not publish a release while package-index ownership is unresolved.
