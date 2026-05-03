"""Type primitives shared by the optimization samplers (gh#120)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ParamSpec:
    """Declarative search-space entry for one parameter.

    Either ``choices`` *or* ``low``/``high`` must be set, not both.
    ``log`` only applies to the continuous case.
    """

    name: str
    choices: tuple[Any, ...] | None = None
    low: float | None = None
    high: float | None = None
    log: bool = False

    def __post_init__(self) -> None:
        has_choices = self.choices is not None
        has_range = self.low is not None and self.high is not None
        if has_choices == has_range:
            raise ValueError(
                f"ParamSpec({self.name!r}): set exactly one of `choices` or `low`+`high`"
            )
        if has_range:
            assert self.low is not None
            assert self.high is not None
            if self.low > self.high:
                raise ValueError(f"ParamSpec({self.name!r}): low must be <= high")
            if self.log and (self.low <= 0 or self.high <= 0):
                raise ValueError(f"ParamSpec({self.name!r}): log range requires positive bounds")

    @property
    def is_discrete(self) -> bool:
        return self.choices is not None


@dataclass(frozen=True)
class Trial:
    """One parameter assignment evaluated against the objective."""

    index: int
    params: dict[str, Any]
    score: float | None = None
    error: str | None = None


Direction = Literal["maximize", "minimize"]


@dataclass(frozen=True)
class StudyResult:
    """Aggregate result of an optimization run."""

    trials: tuple[Trial, ...]
    best: Trial | None
    direction: Direction
    sampler: str

    @property
    def succeeded(self) -> tuple[Trial, ...]:
        return tuple(t for t in self.trials if t.error is None and t.score is not None)

    @property
    def failed(self) -> tuple[Trial, ...]:
        return tuple(t for t in self.trials if t.error is not None)
