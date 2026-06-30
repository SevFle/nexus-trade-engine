from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _pyproject_version() -> str:
    content = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    assert match, "version not found in pyproject.toml"
    return match.group(1)


def _package_json_version() -> str:
    pkg = _read_json(ROOT / "frontend" / "package.json")
    return pkg["version"]


def _manifest_version() -> str:
    manifest = _read_json(ROOT / ".release-please-manifest.json")
    return manifest["."]


class TestReleasePleaseConfig:
    def test_config_parses(self):
        cfg = _read_json(ROOT / "release-please-config.json")
        assert cfg["release-type"] == "python"
        assert cfg["include-v-in-tag"] is True
        assert cfg["bump-minor-pre-major"] is True

    def test_config_changelog_sections(self):
        cfg = _read_json(ROOT / "release-please-config.json")
        sections = cfg["changelog-sections"]
        types = {s["type"] for s in sections}
        expected = {
            "feat",
            "fix",
            "perf",
            "refactor",
            "docs",
            "test",
            "build",
            "ci",
            "chore",
            "revert",
        }
        assert types == expected

    def test_config_extra_files_includes_frontend_package_json(self):
        cfg = _read_json(ROOT / "release-please-config.json")
        pkg = cfg["packages"]["."]
        extra_files = pkg["extra-files"]
        paths = [f.get("path") if isinstance(f, dict) else f for f in extra_files]
        assert "frontend/package.json" in paths

    def test_manifest_parses(self):
        manifest = _read_json(ROOT / ".release-please-manifest.json")
        assert "." in manifest
        version = manifest["."]
        assert re.match(r"^\d+\.\d+\.\d+$", version)


class TestVersionConsistency:
    def test_versions_match_across_files(self):
        v_pyproject = _pyproject_version()
        v_package_json = _package_json_version()
        v_manifest = _manifest_version()
        assert v_pyproject == v_package_json, (
            f"pyproject.toml ({v_pyproject}) != frontend/package.json ({v_package_json})"
        )
        assert v_pyproject == v_manifest, (
            f"pyproject.toml ({v_pyproject}) != .release-please-manifest.json ({v_manifest})"
        )


class TestChangelog:
    def test_changelog_exists(self):
        assert (ROOT / "CHANGELOG.md").is_file()

    def test_changelog_has_keep_a_changelog_header(self):
        content = (ROOT / "CHANGELOG.md").read_text()
        assert "# Changelog" in content
        assert "Keep a Changelog" in content

    def test_changelog_has_unreleased_section(self):
        content = (ROOT / "CHANGELOG.md").read_text()
        assert "## [Unreleased]" in content


class TestReleaseWorkflow:
    def _workflow(self) -> dict:
        return yaml.safe_load((ROOT / ".github" / "workflows" / "release-please.yml").read_text())

    def _triggers(self) -> dict:
        # YAML 1.1 parses a bare ``on:`` key as the boolean ``True``; handle
        # both spellings so the assertion works regardless of loader behavior.
        wf = self._workflow()
        return wf.get("on") or wf.get(True)

    def _steps(self) -> list[dict]:
        return self._workflow()["jobs"]["release-please"]["steps"]

    def test_workflow_file_exists(self):
        assert (ROOT / ".github" / "workflows" / "release-please.yml").is_file()

    def test_workflow_triggers_on_push_to_main(self):
        triggers = self._triggers()
        assert "push" in triggers
        assert "main" in triggers["push"]["branches"]

    def test_workflow_uses_release_please_v4(self):
        uses = [s["uses"] for s in self._steps() if "uses" in s]
        assert "googleapis/release-please-action@v4" in uses

    def test_workflow_references_config_and_manifest(self):
        withs = [s["with"] for s in self._steps() if "with" in s]
        config_files = [w.get("config-file") for w in withs]
        manifest_files = [w.get("manifest-file") for w in withs]
        assert "release-please-config.json" in config_files
        assert ".release-please-manifest.json" in manifest_files

    def test_workflow_has_write_permissions(self):
        perms = self._workflow()["permissions"]
        assert perms["contents"] == "write"
        assert perms["pull-requests"] == "write"

    def test_workflow_uses_custom_token(self):
        withs = [s["with"] for s in self._steps() if "with" in s]
        tokens = [w.get("token", "") for w in withs]
        assert any("RELEASE_PLEASE_TOKEN" in t for t in tokens)

    def test_workflow_step_structure(self):
        # Assert the actual step structure: a single release-please action step
        # carrying token/config/manifest inputs.
        steps = self._steps()
        assert len(steps) >= 1
        rp = next(
            s for s in steps if s.get("uses", "").startswith("googleapis/release-please-action")
        )
        assert rp["with"]["config-file"] == "release-please-config.json"
        assert rp["with"]["manifest-file"] == ".release-please-manifest.json"
        assert "RELEASE_PLEASE_TOKEN" in rp["with"]["token"]

    def test_workflow_skips_when_release_token_absent(self):
        # PyYAML-based structural assertion: parse the workflow file and assert
        # the *actual* ``if:`` expression on the release-please job equals the
        # expected guard exactly. This replaces fragile string-matching / grep
        # approaches with a structural YAML assertion that inspects the parsed
        # tree, so reformatting whitespace or key ordering cannot mask a missing
        # guard.
        job = self._workflow()["jobs"]["release-please"]
        expected_if = "${{ secrets.RELEASE_PLEASE_TOKEN != '' }}"
        assert job["if"] == expected_if, (
            f"release-please job must skip when token is absent; "
            f"expected if={expected_if!r}, got if={job.get('if')!r}"
        )


class TestReleasingDocs:
    def test_releasing_doc_exists(self):
        assert (ROOT / "docs" / "RELEASING.md").is_file()

    def test_releasing_doc_covers_versioning(self):
        content = (ROOT / "docs" / "RELEASING.md").read_text()
        assert "Semantic Versioning" in content
        assert "bump-minor-pre-major" in content

    def test_releasing_doc_covers_conventional_commits(self):
        content = (ROOT / "docs" / "RELEASING.md").read_text()
        assert "Conventional Commits" in content
        assert "feat" in content
        assert "fix" in content

    def test_releasing_doc_covers_troubleshooting(self):
        content = (ROOT / "docs" / "RELEASING.md").read_text()
        assert "Troubleshooting" in content
