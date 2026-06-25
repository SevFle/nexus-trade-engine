"""Roadmap tracking infrastructure for umbrella issue #166.

This module is the programmatic front-end for the post-gap-analysis roadmap
manifest stored next to it as ``roadmap.yaml``. The manifest encodes the six
delivery buckets, the 57 underlying work issues, the key dependency chains,
and the exit criteria. Issue *status* (open / closed) deliberately lives in
GitHub, not in the manifest — so this module keeps the two concerns apart:

* :func:`load_roadmap` parses the YAML into a structurally-validated
  :class:`Roadmap`. Structural validation is exhaustive: exactly 57 issues,
  the contiguous range #109-#165, no duplicate numbers, every issue belongs
  to exactly one bucket, every dependency-chain reference resolves, etc. A
  manifest that fails validation raises :class:`RoadmapValidationError` so a
  bad edit never ships silently.

* Runtime status is supplied separately via :meth:`Roadmap.with_statuses`
  (a mapping of issue number → closed?) and the analysis helpers
  (:meth:`Roadmap.completion`, :meth:`Roadmap.chain_head`,
  :meth:`Roadmap.is_bucket_exited`) consume it. ``Roadmap`` itself is the
  only thing mutated, and only its ``statuses`` map — the buckets, issues,
  and chains are immutable.

The intended wiring (not implemented here, to avoid a hard GitHub API
dependency) is: a small job fetches issue open/closed state and feeds it to
:meth:`Roadmap.with_statuses`; ``scripts/roadmap_status.py`` then renders the
status report (or :meth:`Roadmap.to_markdown` directly) for the umbrella
issue and dashboards. Until that exists, callers can pass an explicit status
map (e.g. from a local cache) and everything works.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

# Umbrella issue this roadmap tracks. Baking it into the module (and
# validating the manifest against it) means a renamed tracking issue is a
# loud failure rather than a silent drift.
TRACKING_ISSUE = 166

# The roadmap is, by design, a fixed 57-issue expansion (#109-#165). Pinning
# the expected total AND the exact contiguous range makes a deleted/added/
# renumbered issue a hard validation error.
EXPECTED_ISSUE_COUNT = 57
EXPECTED_ISSUE_RANGE = range(109, 166)  # 109..165 inclusive == 57 issues

# A dependency chain is only meaningful with at least two linked issues.
_MIN_CHAIN_LENGTH = 2

_MANIFEST_PATH = Path(__file__).parent / "roadmap.yaml"


class RoadmapValidationError(Exception):
    """Raised when the manifest is structurally invalid.

    The message aggregates every problem found in one pass so a maintainer
    editing the YAML fixes all of them at once instead of ping-ponging.
    """


@dataclass(frozen=True)
class RoadmapIssue:
    """One underlying work issue."""

    number: int
    title: str
    bucket: str  # bucket label, e.g. "foundation"


@dataclass(frozen=True)
class Bucket:
    """A delivery bucket. Issues reference back by number."""

    label: str
    name: str
    summary: str
    exit_criteria: str
    issues: tuple[int, ...]


@dataclass(frozen=True)
class DependencyChain:
    """An ordered sequence of issues where each depends on the prior."""

    slug: str
    name: str
    issues: tuple[int, ...]
    # Non-issue terminus, e.g. "production" for the safety chain. Purely
    # informational — it is not an issue number and never validated as one.
    terminal: str | None = None


@dataclass(frozen=True)
class RelatedInitiative:
    """A peer initiative tracked under its own umbrella (MCP, multi-asset…)."""

    name: str
    tracking_issue: int | None
    covers: tuple[int, ...]
    note: str


@dataclass(frozen=True)
class CompletionStats:
    """How done something is. ``pct`` is 0-100 inclusive, 0 when total==0."""

    total: int
    done: int

    @property
    def open(self) -> int:
        return self.total - self.done

    @property
    def pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * self.done / self.total, 2)


@dataclass
class Roadmap:
    """The parsed, validated roadmap plus a mutable runtime status view.

    The structural fields are immutable dataclasses; only ``statuses`` is
    mutable and it is the caller's responsibility to keep it in sync with
    GitHub. :meth:`with_statuses` returns an independent copy so analysis
    helpers are side-effect free.
    """

    title: str
    tracking_issue: int
    buckets: tuple[Bucket, ...]
    issues: tuple[RoadmapIssue, ...]
    chains: tuple[DependencyChain, ...]
    initiatives: tuple[RelatedInitiative, ...]
    statuses: dict[int, bool] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Structural lookups (do not consult `statuses`).
    # ------------------------------------------------------------------ #

    @cached_property
    def issue_by_number(self) -> dict[int, RoadmapIssue]:
        return {i.number: i for i in self.issues}

    @cached_property
    def bucket_by_label(self) -> dict[str, Bucket]:
        return {b.label: b for b in self.buckets}

    def get_issue(self, number: int) -> RoadmapIssue:
        try:
            return self.issue_by_number[number]
        except KeyError:
            msg = f"issue #{number} is not on the roadmap"
            raise KeyError(msg) from None

    def get_bucket(self, label: str) -> Bucket:
        try:
            return self.bucket_by_label[label]
        except KeyError:
            msg = f"unknown bucket label: {label!r}"
            raise KeyError(msg) from None

    def get_chain(self, slug: str) -> DependencyChain:
        for chain in self.chains:
            if chain.slug == slug:
                return chain
        msg = f"unknown dependency chain slug: {slug!r}"
        raise KeyError(msg) from None

    @property
    def all_issue_numbers(self) -> tuple[int, ...]:
        return tuple(i.number for i in self.issues)

    # ------------------------------------------------------------------ #
    # Runtime status queries (consult `statuses`).
    # ------------------------------------------------------------------ #

    def is_closed(self, number: int) -> bool:
        """True if issue ``number`` is recorded as closed.

        Unknown numbers are treated as open rather than raising: an
        out-of-band status entry for an issue we later removed should not
        crash a dashboard. Structural correctness is enforced at load time,
        not here.
        """
        return bool(self.statuses.get(number, False))

    def with_statuses(self, statuses: Mapping[int, bool]) -> Roadmap:
        """Return an independent copy with merged statuses.

        Entries already present are overwritten by ``statuses``; entries for
        numbers not on the roadmap are dropped to keep the map honest. The
        original roadmap is untouched.
        """
        known = self.issue_by_number
        merged = dict(self.statuses)
        merged.update({n: bool(v) for n, v in statuses.items() if n in known})
        return replace(self, statuses=merged)

    def completion(self, *, bucket: str | None = None) -> CompletionStats:
        """Completion over a single bucket, or the whole roadmap if ``None``."""
        numbers = self.all_issue_numbers if bucket is None else self.get_bucket(bucket).issues
        total = len(numbers)
        done = sum(1 for n in numbers if self.is_closed(n))
        return CompletionStats(total=total, done=done)

    def is_bucket_exited(self, label: str) -> bool:
        """True when every issue in a bucket is closed (exit criteria met)."""
        stats = self.completion(bucket=label)
        return stats.total > 0 and stats.done == stats.total

    def is_complete(self) -> bool:
        """True when all six buckets have exited (umbrella can close)."""
        return all(self.is_bucket_exited(b.label) for b in self.buckets)

    def chain_progress(self, slug: str) -> CompletionStats:
        """How far along a dependency chain the closed issues reach."""
        chain = self.get_chain(slug)
        total = len(chain.issues)
        done = sum(1 for n in chain.issues if self.is_closed(n))
        return CompletionStats(total=total, done=done)

    def chain_head(self, slug: str) -> RoadmapIssue | None:
        """The currently actionable issue in a chain.

        Returns the first issue whose every predecessor in the chain is
        closed, or ``None`` if the whole chain is done. This is the
        dependency-respecting notion of "what to work on next": an issue
        only becomes actionable once everything before it in the chain
        has landed.
        """
        chain = self.get_chain(slug)
        for idx, number in enumerate(chain.issues):
            if self.is_closed(number):
                continue
            if all(self.is_closed(prev) for prev in chain.issues[:idx]):
                return self.get_issue(number)
        return None

    def chain_blocked(self, slug: str) -> tuple[RoadmapIssue, ...]:
        """Open chain issues whose immediate predecessor is still open."""
        chain = self.get_chain(slug)
        blocked: list[RoadmapIssue] = []
        for idx, number in enumerate(chain.issues):
            if self.is_closed(number):
                continue
            if idx == 0:
                continue
            if not self.is_closed(chain.issues[idx - 1]):
                blocked.append(self.get_issue(number))
        return tuple(blocked)

    def actionable_issues(self) -> tuple[RoadmapIssue, ...]:
        """Open issues that are not blocked by any dependency chain.

        An issue is *actionable* when it is open and is not listed as a
        blocked successor in any dependency chain. This is the union view of
        "what a contributor can pick up right now".
        """
        blocked_numbers: set[int] = set()
        for chain in self.chains:
            for issue in self.chain_blocked(chain.slug):
                blocked_numbers.add(issue.number)
        return tuple(
            issue
            for issue in self.issues
            if not self.is_closed(issue.number) and issue.number not in blocked_numbers
        )

    # ------------------------------------------------------------------ #
    # Rendering.
    # ------------------------------------------------------------------ #

    def to_markdown(self) -> str:
        """Render the roadmap (with current statuses) as a status report.

        The shape mirrors the umbrella issue body: a per-bucket progress
        block, a chain progress table, and the next-actionable issue per
        chain. Markdown checkboxes reflect ``self.statuses`` so a status
        sync job can regenerate this report and diff it against issue #166.
        """
        lines: list[str] = []
        overall = self.completion()
        lines.append(f"# Roadmap status — #{self.tracking_issue}")
        lines.append("")
        lines.append(self.title)
        lines.append("")
        lines.append(
            f"**Overall:** {overall.done}/{overall.total} "
            f"({overall.pct:.1f}%) · "
            f"buckets exited: "
            f"{sum(1 for b in self.buckets if self.is_bucket_exited(b.label))}/{len(self.buckets)}"
        )
        lines.append("")

        for bucket in self.buckets:
            stats = self.completion(bucket=bucket.label)
            state = "✅ exited" if self.is_bucket_exited(bucket.label) else "🚧 in progress"
            lines.append(
                f"## {bucket.name} (`bucket:{bucket.label}`) — "
                f"{stats.done}/{stats.total} ({stats.pct:.1f}%) {state}"
            )
            lines.append("")
            for number in bucket.issues:
                issue = self.get_issue(number)
                mark = "x" if self.is_closed(number) else " "
                lines.append(f"- [{mark}] #{number} {issue.title}")
            lines.append("")
            lines.append(f"> **Exit criteria:** {bucket.exit_criteria.strip()}")
            lines.append("")

        if self.chains:
            lines.append("## Dependency chains")
            lines.append("")
            lines.append("| Chain | Progress | Next actionable |")
            lines.append("|-------|----------|-----------------|")
            for chain in self.chains:
                stats = self.chain_progress(chain.slug)
                head = self.chain_head(chain.slug)
                head_cell = f"#{head.number}" if head is not None else "— (done)"
                lines.append(
                    f"| {chain.name} | {stats.done}/{stats.total} ({stats.pct:.0f}%) "
                    f"| {head_cell} |"
                )
            lines.append("")

        if self.is_complete():
            lines.append("✅ **All buckets exited — umbrella #166 can close.**")
        else:
            lines.append("⏳ Roadmap in progress; umbrella #166 stays open.")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Parsing helpers.
# ---------------------------------------------------------------------- #


def _req_str(raw: Mapping[str, object], key: str, ctx: str) -> str:
    """Fetch ``raw[key]`` as a non-empty str, else raise.

    Returning ``str`` (not ``object``) lets the static checker narrow the
    caller's local, which the inline ``isinstance`` loop trick could not.
    """
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{ctx} {key!r} must be a non-empty string"
        raise RoadmapValidationError(msg)
    return value


def _parse_issue(raw: Mapping[str, object], bucket_label: str) -> RoadmapIssue:
    number = raw["number"]
    title = raw["title"]
    if not isinstance(number, int) or number <= 0:
        msg = f"bucket {bucket_label!r}: issue number must be a positive int, got {number!r}"
        raise RoadmapValidationError(msg)
    if not isinstance(title, str) or not title.strip():
        msg = f"bucket {bucket_label!r}: issue #{number} has an empty title"
        raise RoadmapValidationError(msg)
    return RoadmapIssue(number=number, title=title, bucket=bucket_label)


def _parse_bucket(raw: Mapping[str, object]) -> tuple[Bucket, tuple[RoadmapIssue, ...]]:
    """Parse one bucket; return it together with its parsed issues.

    Returning the issues alongside the bucket avoids a second pass over the
    raw YAML in :func:`load_roadmap` and keeps the issue objects (with their
    titles) available in exactly one place.
    """
    label = _req_str(raw, "label", "bucket")
    name = _req_str(raw, "name", "bucket")
    summary = raw.get("summary", "")
    exit_criteria = raw.get("exit_criteria", "")
    raw_issues = raw.get("issues", [])
    if not isinstance(summary, str):
        msg = f"bucket {label!r}: summary must be a string"
        raise RoadmapValidationError(msg)
    if not isinstance(exit_criteria, str):
        msg = f"bucket {label!r}: exit_criteria must be a string"
        raise RoadmapValidationError(msg)
    if not isinstance(raw_issues, list) or not raw_issues:
        msg = f"bucket {label!r}: must list at least one issue"
        raise RoadmapValidationError(msg)
    issues = tuple(_parse_issue(item, label) for item in raw_issues)
    bucket = Bucket(
        label=label,
        name=name,
        summary=summary,
        exit_criteria=exit_criteria,
        issues=tuple(i.number for i in issues),
    )
    return bucket, issues


def _parse_chain(raw: Mapping[str, object]) -> DependencyChain:
    slug = _req_str(raw, "slug", "chain")
    name = _req_str(raw, "name", "chain")
    raw_issues = raw.get("issues", [])
    terminal = raw.get("terminal")
    if not isinstance(raw_issues, list) or len(raw_issues) < _MIN_CHAIN_LENGTH:
        msg = f"chain {slug!r}: must list at least two issues"
        raise RoadmapValidationError(msg)
    issues = tuple(int(n) for n in raw_issues)
    if terminal is not None and not isinstance(terminal, str):
        msg = f"chain {slug!r}: terminal must be a string or null"
        raise RoadmapValidationError(msg)
    return DependencyChain(slug=slug, name=name, issues=issues, terminal=terminal)


def _parse_initiative(raw: Mapping[str, object]) -> RelatedInitiative:
    name = _req_str(raw, "name", "initiative")
    tracking = raw.get("tracking_issue")
    covers = raw.get("covers", [])
    note = raw.get("note", "")
    if tracking is not None and (not isinstance(tracking, int) or tracking <= 0):
        msg = f"initiative {name!r}: tracking_issue must be a positive int or null"
        raise RoadmapValidationError(msg)
    if not isinstance(covers, list):
        msg = f"initiative {name!r}: covers must be a list"
        raise RoadmapValidationError(msg)
    if not isinstance(note, str):
        msg = f"initiative {name!r}: note must be a string"
        raise RoadmapValidationError(msg)
    return RelatedInitiative(
        name=name,
        tracking_issue=tracking,
        covers=tuple(int(n) for n in covers),
        note=note,
    )


# ---------------------------------------------------------------------- #
# Structural validators. Each returns a list of human-readable errors.
# ---------------------------------------------------------------------- #


def _tracking_issue_errors(roadmap: Roadmap) -> list[str]:
    """Tracking-issue mismatch, if any."""
    if roadmap.tracking_issue != TRACKING_ISSUE:
        return [f"tracking_issue is {roadmap.tracking_issue}, expected {TRACKING_ISSUE}"]
    return []


def _issue_uniqueness_errors(roadmap: Roadmap) -> list[str]:
    """Duplicate issue numbers, the count, and the contiguous-range check."""
    errors: list[str] = []
    numbers = [i.number for i in roadmap.issues]
    dupes = [n for n, count in Counter(numbers).items() if count > 1]
    if dupes:
        errors.append(f"duplicate issue numbers: {sorted(dupes)}")
    if len(numbers) != EXPECTED_ISSUE_COUNT:
        errors.append(f"expected {EXPECTED_ISSUE_COUNT} issues, found {len(numbers)}")
    expected_set = set(EXPECTED_ISSUE_RANGE)
    found_set = set(numbers)
    missing = sorted(expected_set - found_set)
    extra = sorted(found_set - expected_set)
    if missing:
        errors.append(f"missing expected issues from #109-#165: {missing}")
    if extra:
        errors.append(f"unexpected issues outside #109-#165: {extra}")
    return errors


def _bucket_membership_errors(roadmap: Roadmap) -> list[str]:
    """Every issue belongs to exactly one bucket, and buckets list known issues."""
    errors: list[str] = []

    # Every issue must belong to exactly one bucket, and its recorded
    # bucket label must match the bucket that actually lists it.
    owner: dict[int, str] = {}
    for bucket in roadmap.buckets:
        for n in bucket.issues:
            if n in owner:
                errors.append(
                    f"issue #{n} appears in multiple buckets: {owner[n]!r} and {bucket.label!r}"
                )
            owner[n] = bucket.label

    for issue in roadmap.issues:
        bucket = roadmap.bucket_by_label.get(issue.bucket)
        if bucket is None:
            errors.append(f"issue #{issue.number} references unknown bucket {issue.bucket!r}")
        elif issue.number not in bucket.issues:
            errors.append(
                f"issue #{issue.number} claims bucket {issue.bucket!r} but is not listed there"
            )

    for bucket in roadmap.buckets:
        errors.extend(
            f"bucket {bucket.label!r} references unknown issue #{n}"
            for n in bucket.issues
            if n not in roadmap.issue_by_number
        )

    return errors


def _bucket_label_errors(roadmap: Roadmap) -> list[str]:
    """Duplicate bucket labels, if any."""
    labels = [b.label for b in roadmap.buckets]
    label_dupes = [lbl for lbl, count in Counter(labels).items() if count > 1]
    if label_dupes:
        return [f"duplicate bucket labels: {sorted(label_dupes)}"]
    return []


def _chain_errors(roadmap: Roadmap) -> list[str]:
    """Chain references must resolve, have no repeats, and unique slugs."""
    errors: list[str] = []
    chain_slugs = [c.slug for c in roadmap.chains]
    slug_dupes = [s for s, count in Counter(chain_slugs).items() if count > 1]
    if slug_dupes:
        errors.append(f"duplicate chain slugs: {sorted(slug_dupes)}")

    for chain in roadmap.chains:
        if len(set(chain.issues)) != len(chain.issues):
            errors.append(f"chain {chain.slug!r} repeats an issue: {list(chain.issues)}")
        unknown = [n for n in chain.issues if n not in roadmap.issue_by_number]
        if unknown:
            errors.append(f"chain {chain.slug!r} references unknown issues: {unknown}")
    return errors


def _validate_structure(roadmap: Roadmap) -> None:
    """Run the full structural invariant check; raise on any violation."""
    errors: list[str] = []
    errors.extend(_tracking_issue_errors(roadmap))
    errors.extend(_issue_uniqueness_errors(roadmap))
    errors.extend(_bucket_membership_errors(roadmap))
    errors.extend(_bucket_label_errors(roadmap))
    errors.extend(_chain_errors(roadmap))

    if errors:
        joined = "; ".join(errors)
        msg = f"roadmap manifest is invalid ({len(errors)} problem(s)): {joined}"
        raise RoadmapValidationError(msg)


def load_roadmap(path: str | Path | None = None) -> Roadmap:
    """Parse and structurally validate the roadmap manifest.

    By default the bundled ``roadmap.yaml`` is loaded. Pass ``path`` to load
    an alternate manifest (used by tests). A manifest that fails any
    structural check raises :class:`RoadmapValidationError`.
    """
    manifest_path = Path(path) if path is not None else _MANIFEST_PATH
    with manifest_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        msg = "manifest must be a YAML mapping"
        raise RoadmapValidationError(msg)

    tracking_issue = data.get("tracking_issue")
    if not isinstance(tracking_issue, int):
        msg = "tracking_issue must be an int"
        raise RoadmapValidationError(msg)

    raw_buckets = data.get("buckets")
    raw_chains = data.get("chains", [])
    raw_initiatives = data.get("initiatives", [])
    if not isinstance(raw_buckets, list) or not raw_buckets:
        msg = "manifest must define at least one bucket"
        raise RoadmapValidationError(msg)
    if not isinstance(raw_chains, list):
        msg = "chains must be a list"
        raise RoadmapValidationError(msg)
    if not isinstance(raw_initiatives, list):
        msg = "initiatives must be a list"
        raise RoadmapValidationError(msg)

    parsed_buckets: list[Bucket] = []
    issues: list[RoadmapIssue] = []
    for raw_bucket in raw_buckets:
        bucket, bucket_issues = _parse_bucket(raw_bucket)
        parsed_buckets.append(bucket)
        issues.extend(bucket_issues)
    buckets = tuple(parsed_buckets)

    chains = tuple(_parse_chain(c) for c in raw_chains)
    initiatives = tuple(_parse_initiative(i) for i in raw_initiatives)

    roadmap = Roadmap(
        title=data.get("title", ""),
        tracking_issue=tracking_issue,
        buckets=buckets,
        issues=tuple(issues),
        chains=chains,
        initiatives=initiatives,
    )
    _validate_structure(roadmap)
    return roadmap


def load_status_map(path: str | Path) -> dict[int, bool]:
    """Load an issue-number → closed-bool status map from a JSON file.

    The file is expected to be a JSON object mapping the string or integer
    issue number to a boolean (``true`` = closed). Non-boolean values are
    coerced with ``bool()``. This is the companion reader for the status
    sync job that feeds :meth:`Roadmap.with_statuses`.
    """
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        msg = "status file must be a JSON object mapping issue number -> bool"
        raise RoadmapValidationError(msg)
    return {int(k): bool(v) for k, v in data.items()}
