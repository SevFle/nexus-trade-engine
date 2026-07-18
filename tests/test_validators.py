"""Unit tests for :mod:`engine.api.validators` — the shared identifier contract.

These tests pin the contract enforced by :data:`SafeIdentifier` *in
isolation*, without standing up a router or DB. They cover five concerns:

1. **Acceptance** — every legal character class (letters, digits, hyphen,
   underscore) is admitted, including at boundary positions, *and* dots
   are admitted as single separators between non-empty, dot-free tokens
   (e.g. ``a.b``, ``Mean-Reversion_v2.1``).
2. **Rejection** — leading/trailing/consecutive dots, slashes, whitespace,
   punctuation (other than ``-`` ``_`` ``.``), control characters, the
   empty string, and non-ASCII (Unicode) code points are all refused.
3. **Length bounds** — ``max_length`` is enforced at the exact boundary
   (off-by-one safety).
4. **ReDoS safety** — pathological inputs of 10^6+ characters complete in
   bounded (linear) time.
5. **Import consistency** — ``strategies.py`` and ``scoring.py`` source
   the *same* shared constant rather than re-deriving a private regex,
   so the two routes can never drift apart.

These are pure unit tests: they exercise the pattern, the named length
constant, and the bundled :class:`fastapi.Path` marker directly. The
route-level end-to-end behaviour (HTTP 422 on a hostile path parameter)
is pinned separately by ``tests/test_identifier_validation_sev.py``.

Note on the dot contract: the canonical contract
(commit ``a6b6fd24``) is ``^[A-Za-z0-9_-]+(\\.[A-Za-z0-9_-]+)*$`` — i.e.
dots are accepted *only* as single separators between non-empty, dot-free
tokens. A later refactor (commit ``37182317``, not on ``main``) tightened
the pattern to ``^[A-Za-z0-9_-]+$`` and dropped dot support; these tests
pin the original, dot-tolerant contract that ``main`` restored, which is
what the route handlers, registry lookups, and log lines all assume.
"""

from __future__ import annotations

import re
import string
import time
from typing import Annotated, get_args, get_origin

import pytest
from fastapi.params import Path as _PathClass

from engine.api import validators
from engine.api.routes import scoring as scoring_module
from engine.api.routes import strategies as strategies_module
from engine.api.validators import (
    MAX_IDENTIFIER_LENGTH,
    SAFE_IDENTIFIER_PATTERN,
    SafeIdentifier,
)

# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _compiled_pattern() -> re.Pattern[str]:
    """Compile the shared pattern once.

    ``re.fullmatch`` on an already ``^...$``-anchored pattern is
    equivalent to ``re.match`` on the same input — we use ``fullmatch``
    so the test is correct regardless of whether the shared pattern is
    anchored in the future.
    """
    return re.compile(SAFE_IDENTIFIER_PATTERN)


def _matches(value: str) -> bool:
    """Whether *value* satisfies the shared identifier pattern."""
    return _compiled_pattern().fullmatch(value) is not None


# --------------------------------------------------------------------- #
# 1. Acceptance: valid identifier patterns (including dot separators)
# --------------------------------------------------------------------- #


class TestAcceptsValidIdentifiers:
    """The pattern admits every legal character class at every position,
    plus ``.`` as a single separator between non-empty, dot-free tokens."""

    @pytest.mark.parametrize(
        "identifier",
        [
            # Single character of each legal class.
            "a",
            "Z",
            "0",
            "9",
            "-",
            "_",
            # Mixed alphanumeric.
            "abc",
            "ABC",
            "123",
            "Abc123",
            # Hyphens and underscores at the start, middle, and end.
            "-abc",
            "abc-",
            "a-b-c",
            "_abc",
            "abc_",
            "a_b_c",
            # All legal characters combined.
            "Mean-Reversion_v2",
            "strategy_42",
            "a1-b2_c3",
            # Dots as single separators between non-empty tokens.  This
            # is the restored dot-tolerant contract: one or more
            # dot-free tokens joined by single dots.
            "a.b",
            "a.b.c",
            "a1.b2.c3",
            "v1.2",
            "Mean-Reversion_v2.1",
            "strategy.v2.final",
            "lib.sub.mod_42",
            # Dot adjacent to hyphen/underscore inside a token (both
            # legal inside the dot-free token class).
            "a.b-c",
            "a.b_c",
            # Maximum legal length (boundary case — see TestLengthBounds).
            "a" * MAX_IDENTIFIER_LENGTH,
            # Maximum legal length expressed as dotted segments.
            "a" * (MAX_IDENTIFIER_LENGTH - 2) + ".b",
        ],
        ids=lambda v: f"value={v!r}" if len(v) <= 24 else f"len={len(v)}",
    )
    def test_pattern_accepts_valid_identifier(self, identifier: str):
        assert _matches(identifier), f"{identifier!r} should be accepted"

    def test_pattern_matches_whole_string_only(self):
        """The ``^...$`` anchors prevent partial-match acceptance of a
        hostile suffix.  A value with a legal prefix followed by an
        illegal character must be rejected in full."""
        assert not _matches("good-name\x00")
        assert not _matches("good-name\nshadow-log-line")
        assert not _matches("good.name\x00")


# --------------------------------------------------------------------- #
# 2. Rejection: leading/trailing/consecutive dots, slashes, special chars,
#    empty, unicode
# --------------------------------------------------------------------- #


class TestRejectsInvalidIdentifiers:
    """Leading/trailing/consecutive dots, slashes, whitespace, punctuation
    outside ``[-_.]``, control characters, the empty string, and all
    Unicode are refused."""

    @pytest.mark.parametrize(
        "identifier",
        [
            # Dots — only as single separators between non-empty tokens
            # are allowed.  A lone dot, leading/trailing dot, and
            # consecutive dots (``..``) are all structurally rejected.
            ".",
            "name.",
            ".name",
            "a..b",
            "a...b",
            "..",
            "...",
            "a..b.c",
            "a.b..c",
            # Slashes (path traversal / segment splitting).
            "/",
            "a/b",
            "/etc",
            "etc/",
            "../",
            "a//b",
            # Whitespace.
            " ",
            "a b",
            "\t",
            "a\tb",
            "\n",
            "a\nb",
            # Leading/trailing whitespace.
            " abc",
            "abc ",
            # Punctuation that is not in the safe set.
            "a*b",
            "a+b",
            "a(b)",
            "a=b",
            "a@b",
            "a,b",
            "a;b",
            "a:b",
            "a!b",
            "a#b",
            "a$b",
            "a%",
            "a&",
            "a?",
            "[abc]",
            "{abc}",
            # Quotes / markup injection.
            "'><svg onload=alert(1)>",
            'strategy" onerror=alert(1)',
            "normal'; DROP TABLE--",
            # Control characters.
            "\x00",
            "a\x00b",
            "\x1b[2J",  # ANSI escape
            "\x7f",  # DEL
            # Empty string.
            "",
        ],
        ids=lambda v: f"value={v!r}"[:60],
    )
    def test_pattern_rejects_invalid_identifier(self, identifier: str):
        assert not _matches(identifier), f"{identifier!r} should be rejected"

    @pytest.mark.parametrize("char", list(string.punctuation))
    def test_punctuation_classification(self, char: str):
        """Punctuation is classified as follows:

        * ``-`` and ``_`` are *unconditionally* legal — accepted alone,
          at boundaries, and between tokens.
        * ``.`` is a legal *separator* only: rejected alone, rejected
          doubled / leading / trailing, but accepted as a single dot
          between two non-empty dot-free tokens.
        * Every other ASCII punctuation mark is refused everywhere.
        """
        if char in "-_":
            # Unconditionally legal.
            assert _matches(char), f"{char!r} should be accepted"
            assert _matches(f"a{char}b"), f"{char!r} between tokens should be accepted"
            return
        # Alone, every other punctuation (including ``.``) is rejected.
        assert not _matches(char), f"{char!r} alone must be rejected"
        if char == ".":
            # ``.`` is the one legal separator: ``a.b`` is accepted...
            assert _matches("a.b"), "'.' must be accepted as a single separator"
            assert _matches("a.b.c"), "'.' must be accepted between many tokens"
            # ...but it can never open / close / double up.
            assert not _matches("a..b"), "'.' doubled must be rejected"
            assert not _matches(".a"), "leading '.' must be rejected"
            assert not _matches("a."), "trailing '.' must be rejected"
            return
        # Any other punctuation is rejected even between tokens.
        assert not _matches(f"a{char}b"), f"{char!r} between tokens must be rejected"

    @pytest.mark.parametrize(
        "identifier",
        [
            # Common accented / non-Latin letters.
            "stratégie",
            "über",
            "naïve",
            "日本語",
            "한국어",
            "ελληνικά",
            "русский",
            # Emoji.
            "rocket🚀",
            "🚀",
            # Zero-width and combining marks (log-forging / homoglyph risks).
            "a\u200bb",  # zero-width space
            "a\u0301b",  # combining acute accent
            "café",
            # Non-ASCII digits.
            "١٢٣",  # Arabic-Indic digits
            # Non-ASCII whitespace.
            "\u00a0",  # non-breaking space
            "\u3000",  # ideographic space
        ],
    )
    def test_pattern_rejects_non_ascii(self, identifier: str):
        assert not _matches(identifier), f"{identifier!r} must be rejected"

    def test_pattern_rejects_none_and_non_str_at_call_site(self):
        """``re`` raises ``TypeError`` on non-string input rather than
        silently accepting it — defensive check that the validation layer
        cannot be bypassed by a ``None`` payload."""
        with pytest.raises(TypeError):
            _matches(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# 3. Length bounds: max_length boundary enforcement
# --------------------------------------------------------------------- #


class TestLengthBounds:
    """``SafeIdentifier`` declares ``max_length``; the value must equal the
    named constant and the pattern must agree with it at the boundary."""

    def test_constants_have_expected_values(self):
        assert MAX_IDENTIFIER_LENGTH == 64

    def test_min_boundary_is_accepted_by_pattern(self):
        """A single legal character (the implicit minimum) passes."""
        assert _matches("a")

    def test_below_min_boundary_is_rejected_by_pattern(self):
        """The empty string is rejected by the pattern's leading ``+``
        quantifier."""
        assert not _matches("")

    def test_max_boundary_is_accepted_by_pattern(self):
        """Exactly ``MAX_IDENTIFIER_LENGTH`` legal characters pass."""
        value = "a" * MAX_IDENTIFIER_LENGTH
        assert _matches(value)

    def test_max_boundary_with_dots_is_accepted_by_pattern(self):
        """A dotted identifier of exactly ``MAX_IDENTIFIER_LENGTH`` chars
        (dots included) passes the pattern."""
        value = "a" * (MAX_IDENTIFIER_LENGTH - 2) + ".b"
        assert len(value) == MAX_IDENTIFIER_LENGTH
        assert _matches(value)

    def test_above_max_boundary_is_accepted_by_pattern(self):
        """One character past ``MAX_IDENTIFIER_LENGTH`` is accepted by the
        *pattern* — the bare pattern has no upper bound, so capping it is
        the ``max_length`` Path marker's job (exercised below via the
        bundled :class:`fastapi.Path` inspection)."""
        value = "a" * (MAX_IDENTIFIER_LENGTH + 1)
        assert _matches(value)

    def test_path_marker_enforces_max_length(self):
        """The :class:`fastapi.Path` marker bundled into
        :data:`SafeIdentifier` must carry the exact max-length bound."""
        constraints = _path_marker_constraints(SafeIdentifier)
        assert constraints["max_length"] == MAX_IDENTIFIER_LENGTH

    def test_path_marker_carries_the_shared_pattern(self):
        """The Path marker must reference the *same* pattern string as
        :data:`SAFE_IDENTIFIER_PATTERN` — no private copy."""
        constraints = _path_marker_constraints(SafeIdentifier)
        assert constraints["pattern"] == SAFE_IDENTIFIER_PATTERN


# --------------------------------------------------------------------- #
# 4. ReDoS safety with long inputs
# --------------------------------------------------------------------- #


class TestReDoSSafety:
    """The pattern is two positive character classes with ``+`` quantifiers
    joined by a fixed ``.`` literal — no alternation, no nested
    quantifier that can backtrack ambiguously — so matching time is
    linear in the input length.  These tests pin that property on inputs
    large enough to expose any catastrophic backtracking (which would
    blow up to seconds)."""

    # A loose ceiling: any catastrophic-backtracking pattern blows past
    # this by orders of magnitude on 10^5+ chars.
    _LIN_TIME_BUDGET_SECONDS: float = 0.25

    @pytest.mark.parametrize(
        "length",
        [1_000, 10_000, 100_000, 1_000_000],
    )
    def test_long_legal_input_is_linear_time(self, length: int):
        value = "a" * length
        start = time.perf_counter()
        matched = _matches(value)
        elapsed = time.perf_counter() - start
        assert matched
        assert elapsed < self._LIN_TIME_BUDGET_SECONDS, (
            f"matching {length} legal chars took {elapsed:.3f}s — pattern is no longer linear-time"
        )

    @pytest.mark.parametrize(
        "length",
        [1_000, 10_000, 100_000, 1_000_000],
    )
    def test_long_reject_at_tail_is_linear_time(self, length: int):
        """A common ReDoS probe: a long run of *almost*-legal characters
        followed by a single illegal one, forcing a backtrack.  Our
        pattern has no branch to backtrack into, so this is linear."""
        value = "a" * (length - 1) + "!"
        start = time.perf_counter()
        matched = _matches(value)
        elapsed = time.perf_counter() - start
        assert not matched
        assert elapsed < self._LIN_TIME_BUDGET_SECONDS, (
            f"rejecting {length} near-legal chars took {elapsed:.3f}s — "
            "pattern is no longer linear-time"
        )

    @pytest.mark.parametrize(
        "length",
        [1_000, 10_000, 100_000, 1_000_000],
    )
    def test_long_dot_separated_legal_input_is_linear_time(self, length: int):
        """A long, fully-legal dotted identifier (``a.a.a...``) must match
        in linear time — the dot separator never introduces ambiguity."""
        # Build ``a.a.a...`` truncated to ``length`` chars.
        value = ("a." * (length // 2 + 1))[:length]
        # Ensure we ended on a legal token (strip a trailing dot if any).
        if value.endswith("."):
            value = value[:-1] + "a"
        start = time.perf_counter()
        matched = _matches(value)
        elapsed = time.perf_counter() - start
        assert matched, "dotted legal input must be accepted"
        assert elapsed < self._LIN_TIME_BUDGET_SECONDS, (
            f"matching {length} dotted legal chars took {elapsed:.3f}s — "
            "pattern is no longer linear-time"
        )

    def test_long_dot_only_input_is_rejected_quickly(self):
        """A long input built entirely from dots would be the worst case
        for any dot-tolerant pattern; ours rejects on the first
        character."""
        value = "." * 100_000
        start = time.perf_counter()
        matched = _matches(value)
        elapsed = time.perf_counter() - start
        assert not matched
        assert elapsed < self._LIN_TIME_BUDGET_SECONDS

    def test_long_consecutive_dots_after_token_is_rejected_quickly(self):
        """``a`` followed by 10^5 dots — the maximal structural probe for a
        dot-tolerant pattern (potential ambiguous backtracking across the
        ``(\\.[A-Za-z0-9_-]+)*`` group).  Must reject in linear time."""
        value = "a" + "." * 100_000
        start = time.perf_counter()
        matched = _matches(value)
        elapsed = time.perf_counter() - start
        assert not matched
        assert elapsed < self._LIN_TIME_BUDGET_SECONDS

    def test_match_time_grows_at_most_linearly(self):
        """Double the input length, the match time should grow by at
        most a small constant factor (we allow generous slack for CI
        jitter and the per-call regex compile amortisation)."""
        small = "a" * 5_000
        large = "a" * 50_000
        # Warm the compiled-pattern cache so we measure steady-state.
        _matches(small)
        t_small = _timed_match(small)
        t_large = _timed_match(large)
        # Linear scaling → t_large / t_small ≈ 10.  Allow up to ~30x to
        # absorb CI noise; a quadratic or worse pattern would blow past
        # this on 50k vs 5k characters.
        assert t_large < t_small * 30 + self._LIN_TIME_BUDGET_SECONDS, (
            f"non-linear scaling detected: 5k={t_small * 1000:.2f}ms, 50k={t_large * 1000:.2f}ms"
        )


def _timed_match(value: str) -> float:
    start = time.perf_counter()
    _matches(value)
    return time.perf_counter() - start


# --------------------------------------------------------------------- #
# 5. Import consistency between strategies.py and scoring.py
# --------------------------------------------------------------------- #


class TestRouteImportConsistency:
    """Both route modules must source their identifier validator from the
    shared :mod:`engine.api.validators` module — never re-define a
    private pattern.  This is the structural guarantee that the two
    routes cannot drift to different (stricter or looser) contracts."""

    def test_both_modules_import_safe_identifier(self):
        assert hasattr(strategies_module, "SafeIdentifier")
        assert hasattr(scoring_module, "SafeIdentifier")
        assert strategies_module.SafeIdentifier is SafeIdentifier
        assert scoring_module.SafeIdentifier is SafeIdentifier

    def test_neither_module_redefines_a_private_pattern(self):
        """A leftover private ``_SAFE_IDENTIFIER_PATTERN`` (or similar)
        would be a silent drift hazard.  Assert it is gone."""
        private_attrs = [
            name
            for name in dir(strategies_module)
            if "PATTERN" in name.upper() and not name.startswith("__")
        ]
        assert private_attrs == [], (
            f"strategies.py still defines a private pattern: {private_attrs}"
        )
        private_attrs = [
            name
            for name in dir(scoring_module)
            if "PATTERN" in name.upper() and not name.startswith("__")
        ]
        assert private_attrs == [], f"scoring.py still defines a private pattern: {private_attrs}"

    def test_route_handlers_use_safe_identifier_annotation(self):
        """The shared alias must appear as the type annotation of every
        identifier-bearing path parameter on both routers.

        FastAPI unwraps ``Annotated[str, Path(...)]`` into a ``Path``
        field with a ``str`` annotation, so we cannot compare the alias
        directly.  Instead we assert the param's constraint metadata is
        *identical* to the metadata bundled on :data:`SafeIdentifier` —
        i.e. every identifier param on the router enforces the exact
        same pattern and length bounds, sourced from the same alias.
        """
        shared_metadata = _safe_identifier_metadata()

        strategies_params = _collect_identifier_path_params(
            strategies_module.router, {"strategy_id"}, shared_metadata
        )
        assert strategies_params == {"strategy_id"}, (
            f"unexpected identifier params on strategies router: {strategies_params}"
        )

        scoring_params = _collect_identifier_path_params(
            scoring_module.router, {"strategy_name"}, shared_metadata
        )
        assert scoring_params == {"strategy_name"}, (
            f"unexpected identifier params on scoring router: {scoring_params}"
        )

    def test_validators_module_exposes_the_shared_constants(self):
        """Public surface that route modules (and these tests) depend on."""
        assert validators.SAFE_IDENTIFIER_PATTERN is SAFE_IDENTIFIER_PATTERN
        assert validators.MAX_IDENTIFIER_LENGTH is MAX_IDENTIFIER_LENGTH
        assert validators.SafeIdentifier is SafeIdentifier


def _path_marker_constraints(alias: object) -> dict[str, int | str | None]:
    """Pull the max length and pattern off the single
    :class:`fastapi.Path` marker bundled into an
    ``Annotated[str, Path(...)]`` alias.

    FastAPI stores these as pydantic constraint objects
    (``MaxLen`` / ``_PydanticGeneralMetadata``) in the marker's
    ``metadata`` list, so we read them generically rather than reaching
    for attributes that don't exist on the ``Path`` class.  Only the
    constraints that are actually present are populated; ``min_length``
    is intentionally optional because :data:`SafeIdentifier` does not
    declare one (the pattern's leading ``+`` already forbids the empty
    string).
    """
    marker = _path_marker_constraints_marker(alias)
    constraints: dict[str, int | str | None] = {
        "max_length": None,
        "pattern": None,
    }
    for meta in marker.metadata:
        if hasattr(meta, "max_length"):
            constraints["max_length"] = meta.max_length
        elif hasattr(meta, "pattern"):
            constraints["pattern"] = meta.pattern
    assert all(v is not None for v in constraints.values()), (
        f"Path marker missing expected constraints: {constraints}"
    )
    return constraints


def _safe_identifier_metadata() -> list:
    """The pydantic constraint objects (``MaxLen`` /
    ``_PydanticGeneralMetadata``) bundled onto :data:`SafeIdentifier`'s
    ``Path`` marker.  Route handlers must carry this *exact* list."""
    marker = _path_marker_constraints_marker(SafeIdentifier)
    return list(marker.metadata)


def _path_marker_constraints_marker(alias: object) -> _PathClass:
    """Return the single :class:`fastapi.params.Path` marker bundled
    into an ``Annotated[str, Path(...)]`` alias."""
    assert get_origin(alias) is Annotated, "SafeIdentifier must be Annotated[...]"
    args = get_args(alias)
    assert args[0] is str, "SafeIdentifier must annotate str"
    markers = [a for a in args[1:] if isinstance(a, _PathClass)]
    assert len(markers) == 1, "SafeIdentifier must bundle exactly one Path marker"
    return markers[0]


def _collect_identifier_path_params(
    router,
    expected_names: set[str],
    shared_metadata: list,
) -> set[str]:
    """Return the set of path-parameter names on *router* whose field
    metadata is identical to :data:`SafeIdentifier`'s.

    Raises ``AssertionError`` if any *expected* identifier parameter is
    *not* guarded by the shared contract — i.e. if a route silently
    regressed to a bare ``str`` annotation.
    """
    found: set[str] = set()
    for route in router.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for param in dependant.path_params:
            if param.name not in expected_names:
                continue
            assert isinstance(param.field_info, _PathClass), (
                f"param {param.name!r} is not a Path field"
            )
            assert list(param.field_info.metadata) == shared_metadata, (
                f"param {param.name!r} does not use the shared "
                f"SafeIdentifier contract: {list(param.field_info.metadata)}"
            )
            found.add(param.name)
    missing = expected_names - found
    assert not missing, f"identifier params missing SafeIdentifier annotation: {missing}"
    return found
