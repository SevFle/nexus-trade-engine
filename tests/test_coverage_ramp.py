"""Unit tests for the per-module coverage ramp engine (issue #648)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coverage_ramp as cr  # noqa: E402


def _stat(path: str, *, stmts: int, missing: int, pct: float) -> cr.ModuleStat:
    return cr.ModuleStat(path, stmts, missing, pct)


def _cov_payload(files: dict[str, dict]) -> dict:
    """Build a minimal ``coverage json`` payload from {path: {stmts,missing,pct}}."""
    out = {"files": {}}
    for path, f in files.items():
        out["files"][path] = {
            "summary": {
                "num_statements": f["stmts"],
                "missing_lines": f["missing"],
                "percent_covered": f["pct"],
            }
        }
    return out


class TestParseCoverageJson:
    def test_extracts_per_file_stats(self):
        payload = _cov_payload(
            {
                "engine/a.py": {"stmts": 10, "missing": 2, "pct": 80.0},
                "engine/b.py": {"stmts": 4, "missing": 0, "pct": 100.0},
            }
        )
        stats = cr.parse_coverage_json(payload)
        assert set(stats) == {"engine/a.py", "engine/b.py"}
        a = stats["engine/a.py"]
        assert (a.statements, a.missing, a.percent) == (10, 2, 80.0)
        assert a.covered == 8

    def test_drops_zero_statement_files(self):
        payload = _cov_payload(
            {
                "engine/__init__.py": {"stmts": 0, "missing": 0, "pct": 100.0},
                "engine/a.py": {"stmts": 5, "missing": 1, "pct": 80.0},
            }
        )
        stats = cr.parse_coverage_json(payload)
        assert list(stats) == ["engine/a.py"]

    def test_tolerates_missing_summary(self):
        payload = {
            "files": {
                "engine/a.py": {},
                "engine/b.py": {
                    "summary": {"num_statements": 3, "missing_lines": 1, "percent_covered": 66.0}
                },
            }
        }
        stats = cr.parse_coverage_json(payload)
        assert list(stats) == ["engine/b.py"]

    def test_raises_when_no_files_object(self):
        with pytest.raises(TypeError):
            cr.parse_coverage_json({"totals": {}})

    def test_missing_fields_default_to_zero(self):
        payload = {"files": {"engine/a.py": {"summary": {}}}}
        stats = cr.parse_coverage_json(payload)
        # zero statements -> dropped
        assert stats == {}


class TestFloorPct:
    def test_floors_after_subtracting_headroom(self):
        assert cr._floor_pct(92.96, 1.0) == 91
        assert cr._floor_pct(80.0, 1.0) == 79

    def test_floor_not_round(self):
        # 50.7 - 1 = 49.7 -> floor 49 (not 50)
        assert cr._floor_pct(50.7, 1.0) == 49

    def test_clamps_to_zero(self):
        assert cr._floor_pct(0.5, 1.0) == 0
        assert cr._floor_pct(0.0, 1.0) == 0

    def test_clamps_to_hundred(self):
        assert cr._floor_pct(100.0, 1.0) == 99
        assert cr._floor_pct(105.0, 1.0) == 100

    def test_custom_headroom(self):
        assert cr._floor_pct(90.0, 5.0) == 85


class TestRatchetFloors:
    def test_seeds_new_files_at_measured_minus_headroom(self):
        measured = {
            "engine/a.py": _stat("engine/a.py", stmts=100, missing=8, pct=92.0),
        }
        out = cr.ratchet_floors(measured, {}, headroom=1.0)
        assert out == {"engine/a.py": 91}

    def test_is_monotonic_never_lowers(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=100, missing=20, pct=80.0)}
        # existing floor (90) is above the new candidate (79) -> stays 90
        out = cr.ratchet_floors(measured, {"engine/a.py": 90}, headroom=1.0)
        assert out == {"engine/a.py": 90}

    def test_raises_when_coverage_increased(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=100, missing=5, pct=95.0)}
        out = cr.ratchet_floors(measured, {"engine/a.py": 90}, headroom=1.0)
        assert out == {"engine/a.py": 94}

    def test_drops_deleted_files(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=10, missing=1, pct=90.0)}
        out = cr.ratchet_floors(measured, {"engine/gone.py": 50}, headroom=1.0)
        assert "engine/gone.py" not in out
        assert out == {"engine/a.py": 89}

    def test_output_is_sorted_by_path(self):
        measured = {
            "engine/z.py": _stat("engine/z.py", stmts=10, missing=1, pct=90.0),
            "engine/a.py": _stat("engine/a.py", stmts=10, missing=1, pct=90.0),
            "engine/m.py": _stat("engine/m.py", stmts=10, missing=1, pct=90.0),
        }
        out = cr.ratchet_floors(measured, {}, headroom=1.0)
        assert list(out) == ["engine/a.py", "engine/m.py", "engine/z.py"]


class TestCheckFloors:
    def test_no_violations_when_all_above(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=10, missing=1, pct=90.0)}
        floors = {"engine/a.py": 89}
        assert cr.check_floors(measured, floors) == []

    def test_boundary_measured_equals_floor_is_ok(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=10, missing=1, pct=89.0)}
        floors = {"engine/a.py": 89}
        assert cr.check_floors(measured, floors) == []

    def test_flags_below_floor(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=10, missing=5, pct=50.0)}
        floors = {"engine/a.py": 89}
        v = cr.check_floors(measured, floors)
        assert len(v) == 1
        assert v[0]["path"] == "engine/a.py"
        assert v[0]["floor"] == 89
        assert v[0]["measured"] == 50.0
        assert v[0]["shortfall"] == 39.0

    def test_ignores_files_without_a_floor(self):
        measured = {"engine/a.py": _stat("engine/a.py", stmts=10, missing=9, pct=10.0)}
        assert cr.check_floors(measured, {"engine/other.py": 80}) == []

    def test_ignores_floors_for_files_not_measured(self):
        floors = {"engine/gone.py": 80}
        assert cr.check_floors({}, floors) == []

    def test_sorted_largest_shortfall_first(self):
        measured = {
            "engine/a.py": _stat("engine/a.py", stmts=10, missing=5, pct=50.0),
            "engine/b.py": _stat("engine/b.py", stmts=10, missing=7, pct=30.0),
            "engine/c.py": _stat("engine/c.py", stmts=10, missing=8, pct=20.0),
        }
        floors = dict.fromkeys(measured, 90)
        v = cr.check_floors(measured, floors)
        assert [x["path"] for x in v] == ["engine/c.py", "engine/b.py", "engine/a.py"]


class TestDiffFloors:
    def test_no_change_is_empty(self):
        assert cr.diff_floors({"a.py": 80}, {"a.py": 80}) == []

    def test_raised(self):
        changes = cr.diff_floors({"a.py": 80}, {"a.py": 85})
        assert changes == [{"path": "a.py", "kind": "raised", "from": 80, "to": 85}]

    def test_added(self):
        changes = cr.diff_floors({}, {"a.py": 85})
        assert changes == [{"path": "a.py", "kind": "added", "to": 85}]

    def test_dropped(self):
        changes = cr.diff_floors({"a.py": 80}, {})
        assert changes == [{"path": "a.py", "kind": "dropped", "from": 80}]

    def test_sorted_by_path(self):
        changes = cr.diff_floors(
            {"z.py": 10, "a.py": 10},
            {"z.py": 20, "a.py": 20, "m.py": 30},
        )
        assert [c["path"] for c in changes] == ["a.py", "m.py", "z.py"]


class TestFormatBumpReport:
    def test_empty_changes_message(self):
        report = cr.format_bump_report([], old_total=12, new_total=12)
        assert "12 -> 12" in report
        assert "No floor changes" in report

    def test_lists_changes(self):
        changes = [
            {"path": "a.py", "kind": "raised", "from": 80, "to": 85},
            {"path": "m.py", "kind": "added", "to": 90},
            {"path": "z.py", "kind": "dropped", "from": 70},
        ]
        report = cr.format_bump_report(changes, old_total=2, new_total=2)
        assert "raised: 1" in report
        assert "added: 1" in report
        assert "dropped: 1" in report
        assert "a.py: 80 -> 85" in report
        assert "m.py: (new) -> 90" in report
        assert "z.py: removed floor 70" in report


class TestFormatCheckReport:
    def test_pass_report(self):
        report = cr.format_check_report([], total_floors=12, total_measured=40)
        assert report.startswith("OK:")
        assert "12 per-module floors met" in report

    def test_fail_report_lists_violations(self):
        violations = [
            {"path": "engine/a.py", "floor": 90, "measured": 50.0, "shortfall": 40.0},
        ]
        report = cr.format_check_report(violations, total_floors=12, total_measured=40)
        assert report.startswith("FAIL:")
        assert "1 of 12" in report
        assert "engine/a.py | 90 | 50.0 | 40.0" in report
        assert "Do NOT lower" in report


class TestLoadSaveFloors:
    def test_roundtrip_preserves_floors(self, tmp_path):
        path = tmp_path / "floors.json"
        cr.save_floors(str(path), {"engine/a.py": 91, "engine/b.py": 88})
        loaded = cr.load_floors(str(path))
        assert loaded == {"engine/a.py": 91, "engine/b.py": 88}

    def test_save_writes_envelope(self, tmp_path):
        path = tmp_path / "floors.json"
        cr.save_floors(str(path), {"engine/a.py": 91})
        envelope = json.loads(path.read_text())
        assert envelope["schema"] == 1
        assert "description" in envelope
        assert envelope["floors"] == {"engine/a.py": 91}

    def test_save_is_sorted(self, tmp_path):
        path = tmp_path / "floors.json"
        cr.save_floors(str(path), {"z.py": 1, "a.py": 2})
        text = path.read_text()
        assert text.index("a.py") < text.index("z.py")

    def test_load_missing_file_is_empty(self, tmp_path):
        assert cr.load_floors(str(tmp_path / "nope.json")) == {}

    def test_load_accepts_bare_object(self, tmp_path):
        path = tmp_path / "bare.json"
        path.write_text(json.dumps({"a.py": 80}))
        assert cr.load_floors(str(path)) == {"a.py": 80}


class TestMain:
    def _measured(self):
        return {
            "engine/a.py": _stat("engine/a.py", stmts=100, missing=8, pct=92.0),
            "engine/b.py": _stat("engine/b.py", stmts=100, missing=20, pct=80.0),
        }

    def test_seed_dry_run_does_not_write(self, tmp_path, monkeypatch, capsys):
        floors_path = tmp_path / "floors.json"
        monkeypatch.setattr(cr, "read_coverage_json", lambda _p: self._measured())
        monkeypatch.setattr(cr, "load_floors", lambda _p: {})

        rc = cr.main(["--floors", str(floors_path), "seed"])
        assert rc == 0
        assert not floors_path.exists()
        out = capsys.readouterr().out
        assert "Dry run" in out

    def test_seed_apply_writes_floors(self, tmp_path, monkeypatch, capsys):
        floors_path = tmp_path / "floors.json"
        monkeypatch.setattr(cr, "read_coverage_json", lambda _p: self._measured())
        # use the real load_floors/save_floors against tmp_path
        rc = cr.main(["--floors", str(floors_path), "--apply", "seed"])
        assert rc == 0
        loaded = cr.load_floors(str(floors_path))
        assert loaded == {"engine/a.py": 91, "engine/b.py": 79}
        assert "Wrote 2 floors" in capsys.readouterr().out

    def test_bump_is_monotonic(self, tmp_path, monkeypatch):
        floors_path = tmp_path / "floors.json"
        cr.save_floors(str(floors_path), {"engine/a.py": 91, "engine/b.py": 79})
        # coverage dropped for b.py — floor must NOT go down
        measured = {
            "engine/a.py": _stat("engine/a.py", stmts=100, missing=0, pct=100.0),
            "engine/b.py": _stat("engine/b.py", stmts=100, missing=50, pct=50.0),
        }
        monkeypatch.setattr(cr, "read_coverage_json", lambda _p: measured)
        rc = cr.main(["--floors", str(floors_path), "--apply", "bump"])
        assert rc == 0
        loaded = cr.load_floors(str(floors_path))
        assert loaded == {"engine/a.py": 99, "engine/b.py": 79}

    def test_check_passes_when_all_above(self, tmp_path, monkeypatch, capsys):
        floors_path = tmp_path / "floors.json"
        cr.save_floors(str(floors_path), {"engine/a.py": 91, "engine/b.py": 79})
        monkeypatch.setattr(cr, "read_coverage_json", lambda _p: self._measured())
        rc = cr.main(["--floors", str(floors_path), "check"])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_check_fails_on_violation(self, tmp_path, monkeypatch, capsys):
        floors_path = tmp_path / "floors.json"
        cr.save_floors(str(floors_path), {"engine/a.py": 95})
        monkeypatch.setattr(cr, "read_coverage_json", lambda _p: self._measured())
        rc = cr.main(["--floors", str(floors_path), "check"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "FAIL:" in out
        assert "engine/a.py" in out

    def test_apply_env_var_also_writes(self, tmp_path, monkeypatch):
        floors_path = tmp_path / "floors.json"
        monkeypatch.setattr(cr, "read_coverage_json", lambda _p: self._measured())
        monkeypatch.setenv("APPLY", "1")
        rc = cr.main(["--floors", str(floors_path), "seed"])
        assert rc == 0
        assert cr.load_floors(str(floors_path)) == {
            "engine/a.py": 91,
            "engine/b.py": 79,
        }
