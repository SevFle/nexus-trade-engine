#!/usr/bin/env python3
"""Auto-close (or dry-run) kaizen PRs that have merge conflicts.

Backs the ``.github/workflows/conflicting-kaizen.yml`` workflow.

The kaizen autonomous engine opens PRs from ``kaizen/...`` head branches.
When such a PR accumulates merge conflicts the engine cannot self-resolve,
it rots and blocks the merge queue. This script finds those PRs and,
depending on the ``DRY_RUN`` env var, either logs which PRs *would* be
closed (dry-run — the default) or actually closes them with an
explanatory comment (live mode).

The selection / reporting logic is kept free of any GitHub or I/O
dependency so it can be unit tested directly; ``query_open_prs`` /
``close_pr`` / ``main`` are thin shells over the ``gh`` CLI.

See issues #489 (workflow) and #491 (dry-run mode).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# Branch prefix used by the kaizen autonomous engine.
KAIZEN_BRANCH_PREFIX = "kaizen/"

# GitHub ``mergeable`` states. We only act on CONFLICTING — UNKNOWN means
# GitHub has not finished computing mergeability, so closing would risk a
# PR that is actually mergeable.
MERGEABLE_STATE = "MERGEABLE"
CONFLICTING_STATE = "CONFLICTING"
UNKNOWN_STATE = "UNKNOWN"

CLOSE_COMMENT_TEMPLATE = (
    "Closing automatically: this kaizen PR has merge conflicts that the "
    "autonomous engine cannot self-resolve. Please re-open once rebased on "
    "the latest `main`.\n\n"
    "_(triggered by the `conflicting-kaizen` workflow; see issue #491)_"
)

# Field list kept in sync with ``normalize_pr`` so the GraphQL/REST shape
# and the normalised shape never drift.
GH_PR_FIELDS = "number,title,url,headRefName,mergeable"


def is_kaizen_branch(ref: str) -> bool:
    """Return ``True`` for kaizen-engine head branches (prefix ``kaizen/``)."""
    return ref.startswith(KAIZEN_BRANCH_PREFIX)


def parse_dry_run(value: str | None) -> bool:
    """Coerce a ``DRY_RUN`` env-style value to a bool.

    Unset / empty → ``True`` (the workflow defaults to dry-run so we never
    close by accident). An explicit ``false`` / ``0`` / ``no`` / ``off``
    (case-insensitive) opts into live mode. Any other value is treated as
    dry-run for safety.
    """
    if value is None or value.strip() == "":
        return True
    return value.strip().lower() not in {"false", "0", "no", "off"}


def normalize_pr(node: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a raw ``gh pr list --json`` node into a stable dict shape."""
    return {
        "number": int(node["number"]),
        "title": str(node.get("title", "")),
        "url": str(node.get("url", "")),
        "head_ref": str(node.get("headRefName", "")),
        "mergeable": str(node.get("mergeable", UNKNOWN_STATE)).upper(),
    }


def select_conflicting_kaizen_prs(
    prs: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the subset of normalised ``prs`` that are conflicting kaizen PRs.

    A PR is selected iff its head branch is a kaizen branch *and* its
    ``mergeable`` state is exactly ``CONFLICTING``.
    """
    selected: list[dict[str, Any]] = []
    for pr in prs:
        if not is_kaizen_branch(str(pr.get("head_ref", ""))):
            continue
        if pr.get("mergeable") != CONFLICTING_STATE:
            continue
        selected.append(dict(pr))
    return selected


def format_dry_run_report(
    selected: Sequence[Mapping[str, Any]],
    *,
    total_scanned: int,
) -> str:
    """Render the dry-run summary written to the workflow log."""
    lines = [
        f"DRY RUN: would close {len(selected)} conflicting kaizen PR(s) "
        f"(scanned {total_scanned} open PR(s)).",
    ]
    if not selected:
        lines.append("Nothing to close.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Would close:")
    for pr in sorted(selected, key=lambda p: p["number"]):
        lines.append(f"  - #{pr['number']} {pr.get('title', '')} [{pr.get('head_ref', '')}]")
        if pr.get("url"):
            lines.append(f"    {pr['url']}")
    lines.append("")
    lines.append("Re-run the workflow with DRY_RUN=false to close these PRs.")
    return "\n".join(lines)


def build_close_comment(pr: Mapping[str, Any]) -> str:
    """Build the explanatory comment posted when a PR is closed in live mode."""
    ref = pr.get("head_ref", "")
    ref_line = f"\n\nConflicting branch: `{ref}`" if ref else ""
    return CLOSE_COMMENT_TEMPLATE + ref_line


def _run_gh(args: list[str]) -> str:
    """Invoke the ``gh`` CLI and return stdout; raise on non-zero exit."""
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def query_open_prs() -> list[dict[str, Any]]:
    """Return raw ``gh`` PR nodes for the repo's open PRs."""
    payload = _run_gh(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            GH_PR_FIELDS,
        ]
    )
    return json.loads(payload)


def close_pr(number: int, comment: str) -> None:
    """Close PR ``number`` posting ``comment`` (live mode)."""
    _run_gh(["pr", "close", str(number), "--comment", comment])


def main() -> int:
    """Workflow entry point. Returns a process exit code."""
    dry_run = parse_dry_run(os.environ.get("DRY_RUN"))
    raw_prs = query_open_prs()
    normalized = [normalize_pr(node) for node in raw_prs]
    selected = select_conflicting_kaizen_prs(normalized)

    if dry_run:
        print(format_dry_run_report(selected, total_scanned=len(normalized)))
        return 0

    if not selected:
        print("No conflicting kaizen PRs to close.")
        return 0

    closed = 0
    for pr in selected:
        close_pr(int(pr["number"]), build_close_comment(pr))
        print(f"Closed #{pr['number']} ({pr.get('head_ref', '')})")
        closed += 1
    print(f"Closed {closed} conflicting kaizen PR(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
