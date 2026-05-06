from __future__ import annotations

import json
import re
from pathlib import Path

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
        expected = {"feat", "fix", "perf", "refactor", "docs", "test", "build", "ci", "chore", "revert"}
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
    def test_workflow_file_exists(self):
        assert (ROOT / ".github" / "workflows" / "release-please.yml").is_file()

    def test_workflow_triggers_on_push_to_main(self):
        content = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        assert "push:" in content
        assert "main" in content

    def test_workflow_uses_release_please_v4(self):
        content = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        assert "googleapis/release-please-action@v4" in content

    def test_workflow_references_config_and_manifest(self):
        content = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        assert "release-please-config.json" in content
        assert ".release-please-manifest.json" in content

    def test_workflow_has_write_permissions(self):
        content = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        assert "contents: write" in content
        assert "pull-requests: write" in content

    def test_workflow_uses_custom_token(self):
        content = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        assert "RELEASE_PLEASE_TOKEN" in content


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
