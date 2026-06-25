#!/usr/bin/env python3
"""Per-module coverage ramp (ratchet) — issue #648.

The repo already has a *global* coverage floor (`[tool.coverage.report]
fail_under` in ``pyproject.toml``). Issue #648 adds the next layer: a
**per-module** ratchet that

1. records the current per-file coverage as a baseline of floors,
2. lets a weekly CI job *bump* a floor upward whenever the measured
   coverage for that file has gone up (and never down), and
3. fails the build if any file drops below its recorded floor.

The floors live in ``config/coverage-floors.json`` so the baseline is
version-controlled and diffs are reviewable in the bump PR. The
selection / ratchet / check logic is kept free of any I/O so it can be
unit tested directly; ``load_floors`` / ``save_floors`` /
``read_coverage_json`` / ``main`` are thin shells over the filesystem
and the ``coverage`` tool.

The per-module gate is intentionally **permissive by construction**:
floors are seeded at ``measured - headroom`` (default 1 %, rounded
down), so every module starts passing with at least one point of
headroom. The ratchet only ever raises floors, so the gate can only
get stricter over time — a monotonic ramp, matching the policy in
``docs/coverage-ramp.md`` / ADR-0010.

Backs the ``.github/workflows/coverage-ramp.yml`` weekly workflow.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Where the version-controlled per-module floors live. Kept relative to
# the repo root; ``main`` resolves it via Path(__file__).
DEFAULT_FLOORS_PATH = "config/coverage-floors.json"

# Default headroom (in percentage points) between measured coverage and
# the floor we record. Per issue #648 step 2: "current - 1%".
DEFAULT_HEADROOM = 1.0

# Coverage below this measured value is not worth gating (scaffolded /
# stub code). Files seeded below it get a floor of 0 so the ratchet
# picks them up cleanly once they gain real coverage.
MIN_SEED_COVERAGE = 0.0


@dataclass(frozen=True, slots=True)
class ModuleStat:
    """Measured coverage for a single source file.

    A frozen value object: the ratchet / check / diff logic compares
    and sorts these without mutating them, so immutability (and the
    derived ``__hash__`` / ``__eq__``) keep that intent explicit.
    """

    path: str
    statements: int
    missing: int
    percent: float

    @property
    def covered(self) -> int:
        return self.statements - self.missing

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"ModuleStat(path={self.path!r}, statements={self.statements}, "
            f"missing={self.missing}, percent={self.percent:.2f})"
        )


def parse_coverage_json(payload: Mapping[str, object]) -> dict[str, ModuleStat]:
    """Extract per-file ``ModuleStat`` from a ``coverage json`` payload.

    ``coverage json`` emits ``{"files": {path: {"summary": {...}}}}``;
    this flattens it to ``{path: ModuleStat}``. Files with zero
    statements (empty ``__init__.py``) are dropped — they contribute
    nothing to a ramp and would just add noise to the baseline.
    """
    raw_files = payload.get("files")
    if not isinstance(raw_files, Mapping):
        raise TypeError("coverage JSON is missing a 'files' object")

    out: dict[str, ModuleStat] = {}
    for path, info in raw_files.items():
        if not isinstance(info, Mapping):
            continue
        summary = info.get("summary")
        if not isinstance(summary, Mapping):
            continue
        statements = int(summary.get("num_statements", 0))
        # Drop statement-free files (empty __init__.py / package markers).
        if statements <= 0:
            continue
        missing = int(summary.get("missing_lines", 0))
        percent = float(summary.get("percent_covered", 0.0))
        out[str(path)] = ModuleStat(str(path), statements, missing, percent)
    return out


def _floor_pct(percent: float, headroom: float) -> int:
    """``floor(percent - headroom)`` clamped to ``[0, 100]``.

    Floored (not rounded) so the recorded floor always leaves *at least*
    ``headroom`` points of headroom, even after integer truncation.
    """
    value = math.floor(percent - headroom)
    return max(0, min(100, value))


def ratchet_floors(
    measured: Mapping[str, ModuleStat],
    existing: Mapping[str, int],
    *,
    headroom: float = DEFAULT_HEADROOM,
) -> dict[str, int]:
    """Return the next set of per-module floors (monotonic ratchet).

    For every *measured* file the new floor is::

        max(existing_floor, floor(measured - headroom))

    so a floor can only ever go up. Floors for files that are no longer
    measured (deleted) are dropped — their gate is meaningless once the
    code is gone. The result is sorted by path for a stable diff.
    """
    new_floors: dict[str, int] = {}
    for path, stat in measured.items():
        candidate = _floor_pct(stat.percent, headroom)
        previous = int(existing.get(path, 0))
        new_floors[path] = max(previous, candidate)
    return dict(sorted(new_floors.items()))


def check_floors(
    measured: Mapping[str, ModuleStat],
    floors: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Return a per-file violation list for ``measured`` vs ``floors``.

    A file *violates* iff it has a recorded floor and its measured
    coverage is strictly below that floor. The returned dicts are
    sorted by the size of the shortfall (largest first) so the worst
    regressions head the report. Files without a floor and files absent
    from ``measured`` are ignored.
    """
    violations: list[dict[str, Any]] = []
    for path, floor in floors.items():
        stat = measured.get(path)
        if stat is None:
            continue
        if stat.percent < floor:
            violations.append(
                {
                    "path": path,
                    "floor": floor,
                    "measured": round(stat.percent, 2),
                    "shortfall": round(float(floor) - stat.percent, 2),
                }
            )
    violations.sort(key=lambda v: v["shortfall"], reverse=True)
    return violations


def diff_floors(
    old: Mapping[str, int],
    new: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Return the per-file changes between two floor maps (new - old).

    Only files whose floor actually changed are listed. ``kind`` is one
    of ``"raised"``, ``"added"``, ``"dropped"``. The ratchet never
    *lowers* a floor, so ``"raised"`` is the common case; ``"dropped"``
    only happens when a file is deleted between runs.
    """
    changes: list[dict[str, object]] = []
    for path, new_val in new.items():
        if path not in old:
            changes.append({"path": path, "kind": "added", "to": new_val})
        elif new_val != old[path]:
            changes.append({"path": path, "kind": "raised", "from": old[path], "to": new_val})
    changes.extend(
        {"path": path, "kind": "dropped", "from": old[path]} for path in old.keys() - new.keys()
    )
    changes.sort(key=lambda c: str(c["path"]))
    return changes


def format_bump_report(
    changes: Sequence[Mapping[str, Any]],
    *,
    old_total: int,
    new_total: int,
) -> str:
    """Render the dry-run summary for the ``bump`` subcommand."""
    raised = [c for c in changes if c["kind"] == "raised"]
    added = [c for c in changes if c["kind"] == "added"]
    dropped = [c for c in changes if c["kind"] == "dropped"]

    lines = [
        f"Coverage ramp diff: {old_total} -> {new_total} files tracked.",
        f"  raised: {len(raised)}  added: {len(added)}  dropped: {len(dropped)}",
    ]
    if not changes:
        lines.append("No floor changes — coverage did not move the ratchet.")
        return "\n".join(lines)

    for change in sorted(changes, key=lambda c: str(c["path"])):
        kind = change["kind"]
        path = change["path"]
        if kind == "raised":
            lines.append(f"  + {path}: {change['from']} -> {change['to']}")
        elif kind == "added":
            lines.append(f"  + {path}: (new) -> {change['to']}")
        else:  # dropped
            lines.append(f"  - {path}: removed floor {change['from']}")
    return "\n".join(lines)


def format_check_report(
    violations: Sequence[Mapping[str, Any]],
    *,
    total_floors: int,
    total_measured: int,
) -> str:
    """Render the per-module gate report for the ``check`` subcommand."""
    if not violations:
        return f"OK: all {total_floors} per-module floors met ({total_measured} files measured)."
    lines = [
        f"FAIL: {len(violations)} of {total_floors} per-module floors not met:",
        "  path | floor | measured | shortfall",
    ]
    lines.extend(
        f"  {v['path']} | {v['floor']} | {v['measured']} | {v['shortfall']}" for v in violations
    )
    lines.append("")
    lines.append(
        "Add or fix tests for the listed files. Do NOT lower their floors "
        "(see docs/coverage-ramp.md)."
    )
    return "\n".join(lines)


# --- I/O shells (kept thin; not unit-tested directly) ---------------------


def load_floors(path: str) -> dict[str, int]:
    """Read the per-module floor map from ``path``.

    A missing file is treated as an empty map (the seed case) rather
    than an error, so the first run does not need a pre-existing file.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    floors = data.get("floors", data) if isinstance(data, dict) else data
    if not isinstance(floors, dict):
        raise TypeError(f"{path}: expected a JSON object of {{path: floor}}")
    return {str(k): int(v) for k, v in floors.items()}


def save_floors(path: str, floors: Mapping[str, int]) -> None:
    """Write the floor map to ``path`` with a stable, reviewed shape.

    The envelope (``schema`` / ``headroom`` / ``floors``) is what the
    weekly bump PR diffs; keys are sorted for a minimal, reviewable
    diff and ``indent=2`` keeps it git-friendly.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema": 1,
        "description": (
            "Per-module coverage floors (ratchet). Managed by "
            "scripts/coverage_ramp.py; see docs/coverage-ramp.md and "
            "ADR-0010. Do not hand-edit individual values — re-run the "
            "bump so the history stays monotonic."
        ),
        "headroom": DEFAULT_HEADROOM,
        "floors": dict(sorted(floors.items())),
    }
    p.write_text(json.dumps(envelope, indent=2) + "\n")


def read_coverage_json(path: str) -> dict[str, ModuleStat]:
    """Parse a ``coverage json`` file (or ``-`` for fresh collection)."""
    if path == "-":
        payload = json.loads(_collect_coverage_json())
    else:
        payload = json.loads(pathlib.Path(path).read_text())
    return parse_coverage_json(payload)


def _collect_coverage_json() -> str:
    """Re-run coverage and emit its JSON report to stdout (CI only)."""
    # Write the JSON report from an existing .coverage DB (collected by
    # the test step). We do not re-run the suite here.
    result = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coverage_ramp",
        description="Per-module coverage ratchet (issue #648).",
    )
    parser.add_argument(
        "--floors",
        default=DEFAULT_FLOORS_PATH,
        help=f"Floors config path (default: {DEFAULT_FLOORS_PATH}).",
    )
    parser.add_argument(
        "--coverage-json",
        default="-",
        help="coverage json file, or '-' to collect from .coverage (default: '-').",
    )
    parser.add_argument(
        "--headroom",
        type=float,
        default=DEFAULT_HEADROOM,
        help="Points below measured coverage to set the floor (default: 1.0).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="Record initial floors = measured - headroom.")
    sub.add_parser("bump", help="Ratchet floors up to current coverage.")
    sub.add_parser("check", help="Fail if any module is below its floor.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Workflow entry point. Returns a process exit code.

    - ``seed``/``bump`` print a dry-run report by default; set
      ``APPLY=1`` (or pass ``--apply``) to write the new floors.
    - ``check`` exits non-zero iff any module violates its floor.
    """
    parser = _build_parser()
    # ``--apply`` is a global flag on the parent so it works on every
    # subcommand; parse it manually to keep subparser help clean.
    apply_flag = "--apply" in (argv or sys.argv[1:])
    filtered = [a for a in (argv or sys.argv[1:]) if a != "--apply"]
    args = parser.parse_args(filtered)

    measured = read_coverage_json(args.coverage_json)
    existing = load_floors(args.floors)

    if args.command in {"seed", "bump"}:
        new_floors = ratchet_floors(measured, existing, headroom=args.headroom)
        changes = diff_floors(existing, new_floors)
        print(format_bump_report(changes, old_total=len(existing), new_total=len(new_floors)))
        if apply_flag or os.environ.get("APPLY", "").lower() in {"1", "true", "yes"}:
            save_floors(args.floors, new_floors)
            print(f"\nWrote {len(new_floors)} floors to {args.floors}.")
        else:
            print("\nDry run — re-run with --apply (or APPLY=1) to write.")
        return 0

    if args.command == "check":
        violations = check_floors(measured, existing)
        print(
            format_check_report(
                violations,
                total_floors=len(existing),
                total_measured=len(measured),
            )
        )
        return 1 if violations else 0

    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
