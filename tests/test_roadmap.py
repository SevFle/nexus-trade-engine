"""Tests for engine.roadmap — the umbrella #166 roadmap tracker.

Covers:
* loading + structural validation of the bundled manifest,
* the status / completion / chain analysis helpers,
* markdown report rendering,
* manifest validation failures (built from tmp manifests),
* the JSON status-map loader,
* the CLI entry point in scripts/roadmap_status.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engine.roadmap import (
    EXPECTED_ISSUE_COUNT,
    EXPECTED_ISSUE_RANGE,
    TRACKING_ISSUE,
    CompletionStats,
    RoadmapValidationError,
    load_roadmap,
    load_status_map,
)
from engine.roadmap.roadmap import DependencyChain, Roadmap

_MANIFEST = Path(__file__).resolve().parents[1] / "engine" / "roadmap" / "roadmap.yaml"


# --------------------------------------------------------------------------- #
# Bundled-manifest structural invariants.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def roadmap() -> Roadmap:
    return load_roadmap()


def test_bundled_manifest_loads_and_is_valid(roadmap: Roadmap) -> None:
    assert roadmap.tracking_issue == TRACKING_ISSUE == 166
    assert len(roadmap.buckets) == 6
    assert len(roadmap.chains) == 6
    assert len(roadmap.initiatives) == 3


def test_bundled_manifest_has_exactly_57_issues_109_to_165(roadmap: Roadmap) -> None:
    numbers = roadmap.all_issue_numbers
    assert len(numbers) == EXPECTED_ISSUE_COUNT == 57
    assert set(numbers) == set(EXPECTED_ISSUE_RANGE)
    assert min(numbers) == 109
    assert max(numbers) == 165
    assert len(set(numbers)) == len(numbers)  # no dupes


def test_bundled_manifest_bucket_labels_are_stable(roadmap: Roadmap) -> None:
    # These labels are referenced by automation; renames must be intentional.
    assert [b.label for b in roadmap.buckets] == [
        "foundation",
        "safety",
        "trust",
        "productization",
        "scale",
        "community",
    ]


def test_every_issue_belongs_to_its_listed_bucket(roadmap: Roadmap) -> None:
    for bucket in roadmap.buckets:
        for number in bucket.issues:
            issue = roadmap.get_issue(number)
            assert issue.bucket == bucket.label


def test_chain_issues_resolve_and_are_unique(roadmap: Roadmap) -> None:
    for chain in roadmap.chains:
        assert len(chain.issues) >= 2
        assert len(set(chain.issues)) == len(chain.issues)
        for number in chain.issues:
            assert number in roadmap.issue_by_number


def test_known_issue_counts_per_bucket(roadmap: Roadmap) -> None:
    # Bucket sizes: 7 + 10 + 9 + 11 + 12 + 8 == 57.
    counts = {b.label: len(b.issues) for b in roadmap.buckets}
    assert counts == {
        "foundation": 7,
        "safety": 10,
        "trust": 9,
        "productization": 11,
        "scale": 12,
        "community": 8,
    }
    assert sum(counts.values()) == EXPECTED_ISSUE_COUNT


def test_pre_live_safety_chain_matches_issue(roadmap: Roadmap) -> None:
    chain = roadmap.get_chain("pre-live-safety")
    assert chain.issues == (115, 116, 110, 109, 111, 114, 139)
    assert chain.terminal == "production"


def test_default_status_is_everything_open(roadmap: Roadmap) -> None:
    stats = roadmap.completion()
    assert stats == CompletionStats(total=57, done=0)
    assert stats.open == 57
    assert stats.pct == 0.0
    assert roadmap.is_complete() is False
    for bucket in roadmap.buckets:
        assert roadmap.is_bucket_exited(bucket.label) is False


# --------------------------------------------------------------------------- #
# Status map + completion analysis.
# --------------------------------------------------------------------------- #


def test_with_statuses_returns_independent_copy(roadmap: Roadmap) -> None:
    updated = roadmap.with_statuses({115: True})
    assert roadmap.is_closed(115) is False  # original untouched
    assert updated.is_closed(115) is True


def test_with_statuses_drops_unknown_numbers(roadmap: Roadmap) -> None:
    updated = roadmap.with_statuses({115: True, 99999: True})
    assert 99999 not in updated.statuses
    assert updated.is_closed(99999) is False  # unknown -> open, never raises


def test_completion_per_bucket_and_whole(roadmap: Roadmap) -> None:
    foundation = roadmap.get_bucket("foundation")
    closed = set(foundation.issues[:3])  # close 3 of 7
    updated = roadmap.with_statuses(dict.fromkeys(closed, True))
    assert updated.completion(bucket="foundation") == CompletionStats(total=7, done=3)
    assert updated.completion(bucket="foundation").pct == pytest.approx(42.86, abs=0.01)
    assert updated.completion() == CompletionStats(total=57, done=3)
    assert updated.is_bucket_exited("foundation") is False


def test_is_bucket_exited_and_is_complete(roadmap: Roadmap) -> None:
    updated = roadmap.with_statuses(dict.fromkeys(roadmap.all_issue_numbers, True))
    for bucket in updated.buckets:
        assert updated.is_bucket_exited(bucket.label) is True
    assert updated.completion().done == 57
    assert updated.completion().pct == 100.0
    assert updated.is_complete() is True


def test_completion_stats_zero_total_is_zero_pct() -> None:
    assert CompletionStats(total=0, done=0).pct == 0.0
    assert CompletionStats(total=0, done=0).open == 0


# --------------------------------------------------------------------------- #
# Chain analysis.
# --------------------------------------------------------------------------- #


def test_chain_head_returns_first_unblocked_open(roadmap: Roadmap) -> None:
    # pre-live-safety = [115, 116, 110, 109, 111, 114, 139]
    updated = roadmap.with_statuses({115: True, 116: True})
    head = updated.chain_head("pre-live-safety")
    assert head is not None and head.number == 110


def test_chain_head_skips_closed_without_unblocking_successor(roadmap: Roadmap) -> None:
    # Close 110 but not its predecessors -> head is still the first open
    # issue whose predecessors are all closed, which is 115.
    updated = roadmap.with_statuses({110: True})
    head = updated.chain_head("pre-live-safety")
    assert head is not None and head.number == 115


def test_chain_head_none_when_chain_done(roadmap: Roadmap) -> None:
    chain = roadmap.get_chain("observability")  # [145, 146, 147, 144]
    updated = roadmap.with_statuses(dict.fromkeys(chain.issues, True))
    assert updated.chain_head("observability") is None
    assert updated.chain_progress("observability") == CompletionStats(total=4, done=4)


def test_chain_blocked_flags_successor_of_open_predecessor(roadmap: Roadmap) -> None:
    # Nothing closed -> every non-first issue is blocked by its open predecessor.
    blocked = roadmap.chain_blocked("observability")  # [145, 146, 147, 144]
    assert [i.number for i in blocked] == [146, 147, 144]
    # First issue in a chain is never "blocked" by this definition.
    assert 145 not in {i.number for i in blocked}


def test_chain_blocked_empty_when_unblocked(roadmap: Roadmap) -> None:
    chain = roadmap.get_chain("strategy-trust")
    # Close everything except the last; last's predecessor is closed -> unblocked.
    updated = roadmap.with_statuses(dict.fromkeys(chain.issues[:-1], True))
    assert updated.chain_blocked("strategy-trust") == ()


def test_chain_progress_counts_closed_in_chain(roadmap: Roadmap) -> None:
    updated = roadmap.with_statuses({145: True, 146: True})
    assert updated.chain_progress("observability") == CompletionStats(total=4, done=2)


def test_actionable_issues_excludes_chain_blocked(roadmap: Roadmap) -> None:
    # With nothing closed, chain-blocked successors are excluded; chain heads
    # (the first issue of each chain) and all non-chain issues remain.
    actionable = {i.number for i in roadmap.actionable_issues()}
    assert 115 in actionable  # head of pre-live-safety
    assert 113 in actionable  # head of backtest-correctness
    # 116 is blocked by 115 in pre-live-safety -> not actionable.
    assert 116 not in actionable
    # 146 is blocked by 145 in observability -> not actionable.
    assert 146 not in actionable


def test_actionable_issues_shrinks_as_chain_closes(roadmap: Roadmap) -> None:
    baseline = len(roadmap.actionable_issues())
    updated = roadmap.with_statuses({115: True})  # unblocks 116 in safety chain
    assert len(updated.actionable_issues()) >= baseline  # 116 now actionable


def test_get_chain_unknown_slug_raises(roadmap: Roadmap) -> None:
    with pytest.raises(KeyError):
        roadmap.get_chain("nope")


def test_dependency_chain_terminal_is_optional() -> None:
    chain = DependencyChain(slug="x", name="x", issues=(1, 2))
    assert chain.terminal is None


# --------------------------------------------------------------------------- #
# Markdown report.
# --------------------------------------------------------------------------- #


def test_to_markdown_reflects_status(roadmap: Roadmap) -> None:
    updated = roadmap.with_statuses({115: True})
    md = updated.to_markdown()
    assert "# Roadmap status — #166" in md
    assert "bucket:foundation" in md
    assert "[x] #115" in md
    assert "[ ] #116" in md
    assert "umbrella #166 stays open" in md


def test_to_markdown_complete_message_when_done(roadmap: Roadmap) -> None:
    updated = roadmap.with_statuses(dict.fromkeys(roadmap.all_issue_numbers, True))
    md = updated.to_markdown()
    assert "All buckets exited" in md
    assert "100.0%" in md


def test_to_markdown_includes_all_chain_rows(roadmap: Roadmap) -> None:
    md = roadmap.to_markdown()
    assert "## Dependency chains" in md
    for chain in roadmap.chains:
        assert chain.name in md


# --------------------------------------------------------------------------- #
# load_status_map
# --------------------------------------------------------------------------- #


def test_load_status_map_reads_json(tmp_path: Path) -> None:
    # Keys may be strings (JSON) or ints; values are coerced with bool().
    status_file = tmp_path / "s.json"
    status_file.write_text(json.dumps({"115": True, "116": False, "117": 1, "118": 0}))
    statuses = load_status_map(status_file)
    assert statuses == {115: True, 116: False, 117: True, 118: False}


def test_load_status_map_rejects_non_object(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]")
    with pytest.raises(RoadmapValidationError):
        load_status_map(bad)


# --------------------------------------------------------------------------- #
# Manifest validation failures via tmp manifests.
# --------------------------------------------------------------------------- #


def _minimal_valid_manifest() -> dict:
    """A minimal manifest with exactly issues 109-165 so structural checks pass."""
    # Use the real bundled structure but rebuilt to allow surgical mutation.
    return yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_load_rejects_wrong_tracking_issue(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    data["tracking_issue"] = 999
    with pytest.raises(RoadmapValidationError, match="tracking_issue is 999"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_duplicate_issue_numbers(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    # Make #115 appear twice (foundation + safety).
    safety = data["buckets"][1]
    safety["issues"].append({"number": 115, "title": "dup"})
    with pytest.raises(RoadmapValidationError, match="duplicate issue numbers"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_wrong_issue_count(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    # Drop one issue entirely.
    data["buckets"][0]["issues"].pop(0)
    with pytest.raises(RoadmapValidationError, match="expected 57 issues, found 56"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_out_of_range_issue(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    # Renumber the first foundation issue to 1 (out of the 109-165 range).
    data["buckets"][0]["issues"][0]["number"] = 1
    with pytest.raises(RoadmapValidationError, match="unexpected issues outside #109-#165"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_chain_referencing_unknown_issue(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    data["chains"][0]["issues"][-1] = 99999
    with pytest.raises(RoadmapValidationError, match="references unknown issues"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_chain_repeating_issue(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    chain = data["chains"][0]
    chain["issues"] = [chain["issues"][0], chain["issues"][0]]
    with pytest.raises(RoadmapValidationError, match="repeats an issue"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_too_short_chain(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    data["chains"][0]["issues"] = [115]
    with pytest.raises(RoadmapValidationError, match="at least two issues"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_bucket_with_empty_issue_list(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    # Empty a bucket but keep total at 57 by moving its issues elsewhere is
    # fiddly; instead just clear it and rely on the bucket-specific check.
    data["buckets"][5]["issues"] = []
    with pytest.raises(RoadmapValidationError, match="must list at least one issue"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_non_mapping_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(RoadmapValidationError, match="must be a YAML mapping"):
        load_roadmap(path)


def test_load_rejects_duplicate_bucket_labels(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    data["buckets"][1]["label"] = "foundation"
    with pytest.raises(RoadmapValidationError, match="duplicate bucket labels"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_missing_bucket_key(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"tracking_issue": 166}), encoding="utf-8")
    with pytest.raises(RoadmapValidationError, match="at least one bucket"):
        load_roadmap(path)


def test_load_rejects_chain_with_empty_slug(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    data["chains"][0]["slug"] = "   "
    with pytest.raises(RoadmapValidationError, match="chain 'slug' must be a non-empty"):
        load_roadmap(_write(tmp_path, data))


def test_load_rejects_initiative_bad_tracking(tmp_path: Path) -> None:
    data = _minimal_valid_manifest()
    data["initiatives"][0]["tracking_issue"] = -5
    with pytest.raises(RoadmapValidationError, match="tracking_issue must be a positive int"):
        load_roadmap(_write(tmp_path, data))


# --------------------------------------------------------------------------- #
# CLI entry point (scripts/roadmap_status.py).
# --------------------------------------------------------------------------- #


def _cli_main(argv: list[str]) -> int:
    from scripts.roadmap_status import main

    return main(argv)


def test_cli_prints_report_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _cli_main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Roadmap status — #166" in out
    assert "bucket:foundation" in out


def test_cli_writes_report_file(tmp_path: Path) -> None:
    out = tmp_path / "REPORT.md"
    rc = _cli_main(["--out", str(out)])
    assert rc == 0
    assert "# Roadmap status — #166" in out.read_text(encoding="utf-8")


def test_cli_check_exits_nonzero_when_incomplete(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _cli_main(["--check"])
    assert rc == 1
    assert "not complete" in capsys.readouterr().err


def test_cli_check_exits_zero_when_complete(tmp_path: Path) -> None:
    statuses = {str(n): True for n in load_roadmap().all_issue_numbers}
    status_file = tmp_path / "s.json"
    status_file.write_text(json.dumps(statuses), encoding="utf-8")
    out = tmp_path / "REPORT.md"
    rc = _cli_main(["--statuses", str(status_file), "--out", str(out), "--check"])
    assert rc == 0


def test_cli_invalid_manifest_exits_two(tmp_path: Path) -> None:
    bad = tmp_path / "manifest.yaml"
    bad.write_text("tracking_issue: 1\n", encoding="utf-8")
    rc = _cli_main(["--manifest", str(bad)])
    assert rc == 2
