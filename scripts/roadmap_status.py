#!/usr/bin/env python3
"""Render a roadmap status report for umbrella issue #166.

This is the operator front-end for :mod:`engine.roadmap`. It loads the
bundled manifest, optionally merges a JSON status map (issue number → bool,
``true`` = closed), and writes a Markdown report to stdout or a file. The
report's shape mirrors the umbrella issue body, so a status-sync job can
diff the output against issue #166 to detect checkbox drift.

Usage
-----
    # No status map → structural report (everything shown as open).
    uv run python scripts/roadmap_status.py

    # Merge live issue state and write the report.
    uv run python scripts/roadmap_status.py \
        --statuses statuses.json \
        --out ROADMAP_STATUS.md

The ``--statuses`` file is a JSON object like::

    {"115": true, "116": false}

See :func:`engine.roadmap.load_status_map` for the exact contract.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from engine.roadmap import RoadmapValidationError, load_roadmap, load_status_map


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the umbrella #166 roadmap status report.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to an alternate roadmap.yaml (default: bundled manifest).",
    )
    parser.add_argument(
        "--statuses",
        type=Path,
        default=None,
        help="JSON file mapping issue number -> bool (true = closed).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the report here instead of stdout.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero when the roadmap is not fully complete. "
            "Useful as a CI gate for 'all buckets exited'."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        roadmap = load_roadmap(args.manifest)
    except RoadmapValidationError as exc:
        print(f"error: roadmap manifest is invalid: {exc}", file=sys.stderr)
        return 2

    if args.statuses is not None:
        try:
            statuses = load_status_map(args.statuses)
        except (RoadmapValidationError, OSError, ValueError) as exc:
            print(f"error: could not load statuses: {exc}", file=sys.stderr)
            return 2
        roadmap = roadmap.with_statuses(statuses)

    report = roadmap.to_markdown()

    if args.out is not None:
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(report)
        if not report.endswith("\n"):
            sys.stdout.write("\n")

    if args.check and not roadmap.is_complete():
        overall = roadmap.completion()
        print(
            f"roadmap not complete: {overall.done}/{overall.total} ({overall.pct:.1f}%)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
