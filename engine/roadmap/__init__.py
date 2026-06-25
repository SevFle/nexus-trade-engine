"""Roadmap tracking package (umbrella issue #166).

Re-exports the public API of :mod:`engine.roadmap.roadmap` so callers can do
``from engine.roadmap import load_roadmap, Roadmap`` without reaching into the
implementation module.
"""

from __future__ import annotations

from engine.roadmap.roadmap import (
    EXPECTED_ISSUE_COUNT,
    EXPECTED_ISSUE_RANGE,
    TRACKING_ISSUE,
    Bucket,
    CompletionStats,
    DependencyChain,
    RelatedInitiative,
    Roadmap,
    RoadmapIssue,
    RoadmapValidationError,
    load_roadmap,
    load_status_map,
)

__all__ = [
    "EXPECTED_ISSUE_COUNT",
    "EXPECTED_ISSUE_RANGE",
    "TRACKING_ISSUE",
    "Bucket",
    "CompletionStats",
    "DependencyChain",
    "RelatedInitiative",
    "Roadmap",
    "RoadmapIssue",
    "RoadmapValidationError",
    "load_roadmap",
    "load_status_map",
]
