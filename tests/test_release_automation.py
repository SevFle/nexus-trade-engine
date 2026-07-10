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

    def test_workflow_pins_release_please_action_to_sha(self):
        # Supply-chain safety: pin to an immutable commit SHA rather than a
        # floating tag (``@v4``) that can be moved or hijacked out from under
        # us. The ref after ``@`` must be a full 40-char hex SHA.
        uses = [s["uses"] for s in self._steps() if "uses" in s]
        rp = next(u for u in uses if u.startswith("googleapis/release-please-action"))
        ref = rp.split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            "release-please-action must be pinned to a 40-char commit SHA, "
            f"not a floating tag; got ref={ref!r}"
        )
        # The moving ``@v4`` tag currently resolves to v4.4.1 at this commit.
        assert ref == "5c625bfb5d1ff62eadeeb3772007f7f66fdcf071", (
            f"expected release-please-action pinned to v4.4.1 SHA, got {ref!r}"
        )

    def test_workflow_action_pin_has_version_comment(self):
        # YAML drops comments on parse, so inspect the raw text for an inline
        # version annotation next to the pinned action (e.g. ``# v4.4.1``).
        # This keeps the otherwise-opaque SHA human-readable.
        text = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        line = next(ln for ln in text.splitlines() if "release-please-action@" in ln)
        assert re.search(r"#\s*v\d+\.\d+\.\d+", line), (
            "pinned action should carry a version comment so the SHA is "
            f"human-readable; got line={line!r}"
        )

    def test_workflow_does_not_expose_token_via_job_env(self):
        # The token must NOT be copied into a job-level ``env:`` block. It is
        # tested directly via the ``secrets`` context in the step-level ``if:``,
        # which is both simpler and avoids needless secret surface area.
        job = self._workflow()["jobs"]["release-please"]
        env = job.get("env", {})
        assert "RELEASE_PLEASE_TOKEN" not in env, (
            "release-please job must not expose RELEASE_PLEASE_TOKEN via a "
            f"job-level env block; guard the step via secrets instead. env={env!r}"
        )

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
        # The ``secrets`` context IS permitted inside a step-level ``if:``
        # conditional (the restriction only applies to job-level ``if:``).
        # Therefore the release-please step guards directly on the secret
        # without any job-level ``env:`` indirection.
        job = self._workflow()["jobs"]["release-please"]

        # The release-please step must skip when the token is absent, using the
        # ``secrets`` context directly.
        rp = next(
            s
            for s in job["steps"]
            if s.get("uses", "").startswith("googleapis/release-please-action")
        )
        expected = "${{ secrets.RELEASE_PLEASE_TOKEN != '' }}"
        assert rp.get("if") == expected, (
            "release-please step must skip when token is absent; "
            f"expected if={expected!r}, got if={rp.get('if')!r}"
        )

    def test_workflow_no_env_based_token_pattern_remains(self):
        # Regression guard: ensure the obsolete ``env.RELEASE_PLEASE_TOKEN``
        # guard (which relied on a job-level env copy of the secret) has not
        # crept back in anywhere in the workflow file.
        text = (ROOT / ".github" / "workflows" / "release-please.yml").read_text()
        assert "env.RELEASE_PLEASE_TOKEN" not in text, (
            "workflow must not reference the token via the env context; "
            "use the secrets context directly in the step-level if:"
        )
        assert not re.search(r"^\s*env:\s*$", text, re.MULTILINE), (
            "workflow must not declare a job-level env block for the token"
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
