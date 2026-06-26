"""Unit tests for the conflicting-kaizen auto-close dry-run logic (gh#491)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import close_conflicting_kaizen_prs as cck  # noqa: E402


def _pr(number, *, ref="kaizen/issue-1-x", mergeable="CONFLICTING", title="t", url=""):
    return {
        "number": number,
        "title": title,
        "url": url,
        "head_ref": ref,
        "mergeable": mergeable,
    }


class TestIsKaizenBranch:
    def test_prefix_match(self):
        assert cck.is_kaizen_branch("kaizen/issue-491-dry-run")

    def test_non_match(self):
        assert not cck.is_kaizen_branch("feature/x")
        assert not cck.is_kaizen_branch("")
        assert not cck.is_kaizen_branch("kaizen")  # prefix needs the slash

    def test_does_not_match_embedded_token(self):
        assert not cck.is_kaizen_branch("feat/kaizen-like")


class TestParseDryRun:
    def test_unset_defaults_to_dry_run(self):
        assert cck.parse_dry_run(None) is True

    def test_empty_defaults_to_dry_run(self):
        assert cck.parse_dry_run("   ") is True

    @pytest.mark.parametrize("val", ["false", "False", "FALSE", "0", "no", "off", "Off"])
    def test_falsey_enables_live(self, val):
        assert cck.parse_dry_run(val) is False

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "True", "anything"])
    def test_truthy_keeps_dry_run(self, val):
        assert cck.parse_dry_run(val) is True


class TestNormalizePr:
    def test_maps_camel_case_fields(self):
        node = {
            "number": 42,
            "title": "Fix X",
            "url": "https://x",
            "headRefName": "kaizen/issue-42",
            "mergeable": "CONFLICTING",
        }
        out = cck.normalize_pr(node)
        assert out == {
            "number": 42,
            "title": "Fix X",
            "url": "https://x",
            "head_ref": "kaizen/issue-42",
            "mergeable": "CONFLICTING",
        }

    def test_defaults_and_uppercases_mergeable(self):
        out = cck.normalize_pr({"number": 1, "mergeable": "conflicting"})
        assert out["mergeable"] == "CONFLICTING"
        assert out["head_ref"] == ""
        assert out["title"] == ""


class TestSelectConflictingKaizenPrs:
    def test_selects_only_kaizen_and_conflicting(self):
        prs = [
            _pr(1, ref="kaizen/issue-1", mergeable="CONFLICTING"),
            _pr(2, ref="kaizen/issue-2", mergeable="MERGEABLE"),
            _pr(3, ref="kaizen/issue-3", mergeable="UNKNOWN"),
            _pr(4, ref="feature/a", mergeable="CONFLICTING"),
            _pr(5, ref="kaizen/issue-5", mergeable="CONFLICTING", url="https://x"),
        ]
        selected = cck.select_conflicting_kaizen_prs(prs)
        assert [p["number"] for p in selected] == [1, 5]

    def test_empty_input(self):
        assert cck.select_conflicting_kaizen_prs([]) == []

    def test_does_not_mutate_input(self):
        prs = [_pr(1, mergeable="CONFLICTING")]
        snapshot = [dict(p) for p in prs]
        cck.select_conflicting_kaizen_prs(prs)
        assert prs == snapshot


class TestFormatDryRunReport:
    def test_empty_report(self):
        report = cck.format_dry_run_report([], total_scanned=3)
        assert "would close 0 conflicting kaizen PR(s)" in report
        assert "scanned 3 open PR(s)" in report
        assert "Nothing to close." in report

    def test_lists_selected_prs_sorted_by_number(self):
        selected = [
            _pr(7, title="B", url="https://b"),
            _pr(3, title="A", url="https://a"),
        ]
        report = cck.format_dry_run_report(selected, total_scanned=10)
        # sorted ascending by number
        assert report.index("#3") < report.index("#7")
        assert "would close 2 conflicting kaizen PR(s)" in report
        assert "scanned 10 open PR(s)" in report
        assert "DRY_RUN=false" in report
        assert "https://a" in report and "https://b" in report


class TestBuildCloseComment:
    def test_mentions_conflict_and_omits_branch_when_absent(self):
        comment = cck.build_close_comment({})
        assert "merge conflicts" in comment
        assert "#491" in comment
        assert "Conflicting branch" not in comment

    def test_includes_branch_when_present(self):
        comment = cck.build_close_comment({"head_ref": "kaizen/issue-9"})
        assert "Conflicting branch: `kaizen/issue-9`" in comment


class TestMain:
    def test_dry_run_logs_without_closing(self, monkeypatch, capsys):
        closed: list = []

        def fake_query():
            return [
                {
                    "number": 1,
                    "title": "A",
                    "url": "https://1",
                    "headRefName": "kaizen/issue-1",
                    "mergeable": "CONFLICTING",
                },
                {
                    "number": 2,
                    "title": "B",
                    "url": "https://2",
                    "headRefName": "kaizen/issue-2",
                    "mergeable": "MERGEABLE",
                },
            ]

        monkeypatch.setattr(cck, "query_open_prs", fake_query)
        monkeypatch.setattr(cck, "close_pr", lambda n, c: closed.append((n, c)))
        monkeypatch.setenv("DRY_RUN", "true")

        assert cck.main() == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "#1" in out
        # the mergeable kaizen PR is NOT listed as would-close
        assert "#2" not in out
        assert closed == []  # nothing actually closed

    def test_live_mode_closes_only_conflicting(self, monkeypatch, capsys):
        closed: list = []

        def fake_query():
            return [
                {
                    "number": 10,
                    "title": "A",
                    "url": "u1",
                    "headRefName": "kaizen/issue-10",
                    "mergeable": "CONFLICTING",
                },
                {
                    "number": 11,
                    "title": "B",
                    "url": "u2",
                    "headRefName": "feature/x",
                    "mergeable": "CONFLICTING",
                },
                {
                    "number": 12,
                    "title": "C",
                    "url": "u3",
                    "headRefName": "kaizen/issue-12",
                    "mergeable": "CONFLICTING",
                },
            ]

        monkeypatch.setattr(cck, "query_open_prs", fake_query)
        monkeypatch.setattr(cck, "close_pr", lambda n, c: closed.append((n, c)))
        monkeypatch.setenv("DRY_RUN", "false")

        assert cck.main() == 0
        numbers = [n for n, _ in closed]
        assert numbers == [10, 12]
        # every close call carries the explanatory comment
        for _, comment in closed:
            assert "merge conflicts" in comment
        out = capsys.readouterr().out
        assert "Closed 2 conflicting kaizen PR(s)" in out

    def test_live_mode_with_no_matches(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cck,
            "query_open_prs",
            lambda: [{"number": 1, "headRefName": "feature/x", "mergeable": "CONFLICTING"}],
        )
        closed = []
        monkeypatch.setattr(cck, "close_pr", lambda n, c: closed.append(n))
        monkeypatch.setenv("DRY_RUN", "false")

        assert cck.main() == 0
        assert closed == []
        assert "No conflicting kaizen PRs to close." in capsys.readouterr().out

    def test_unset_dry_run_env_defaults_to_dry_run(self, monkeypatch, capsys):
        monkeypatch.delenv("DRY_RUN", raising=False)
        monkeypatch.setattr(
            cck,
            "query_open_prs",
            lambda: [{"number": 1, "headRefName": "kaizen/issue-1", "mergeable": "CONFLICTING"}],
        )
        closed = []
        monkeypatch.setattr(cck, "close_pr", lambda n, c: closed.append(n))

        assert cck.main() == 0
        assert closed == []
        assert "DRY RUN" in capsys.readouterr().out
