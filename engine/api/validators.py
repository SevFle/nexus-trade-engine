"""Shared FastAPI validators for user-controlled identifier parameters.

Strategy, scoring, and plugin identifiers flow from ``Path`` / ``Query``
parameters into registry lookups, DB queries, log lines, and reflected
error ``detail`` strings.  Centralizing the validation contract here means
every route module enforces the *same* rule — there is no risk of one
route drifting to a looser or stricter regex than another — and the
contract is unit-testable in isolation without standing up a router.

Contract for a "safe identifier"
--------------------------------
* Drawn entirely from ``[A-Za-z0-9_-]`` — letters, digits, hyphen,
  underscore.  No other character class is permitted: in particular
  ``.`` and ``/`` are rejected so an identifier can never be mistaken
  for a relative-path component (``./name``, ``name.``, ``a..b``,
  ``a/b``) and cannot smuggle a namespace/version separator past a
  handler that treats them specially.
* Non-empty (:data:`MIN_IDENTIFIER_LENGTH` == 1; an empty path segment
  is rejected before the handler runs).
* Bounded to :data:`MAX_IDENTIFIER_LENGTH` characters so a hostile or
  runaway identifier cannot blow up a log line, DB index, or reflected
  error detail.

Usage
-----
Use :data:`SafeIdentifier` as the type annotation on any path parameter
that accepts one of these tokens::

    from engine.api.validators import SafeIdentifier

    @router.get("/{strategy_id}")
    async def get(strategy_id: SafeIdentifier) -> ...: ...

FastAPI unwraps the ``Annotated`` alias, discovers the bundled
:class:`fastapi.Path` marker, and enforces the pattern and the length
bounds at the validation layer — returning HTTP 422 *before* the handler
runs for any non-conforming value.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

# Length bounds enforced on every identifier.  Mirrored here as named
# constants (rather than inlined as literals in :data:`SafeIdentifier`)
# so tests and callers reference the same values and stay in sync if a
# bound ever changes.
MIN_IDENTIFIER_LENGTH: int = 1
MAX_IDENTIFIER_LENGTH: int = 64

# Shared, anchored pattern.  The ``^...$`` anchors make the contract
# independent of whether the validator uses ``re.match``, ``re.search``,
# or ``re.fullmatch`` (pydantic/FastAPI versions differ).
#
# A single positive character class with a greedy ``+`` quantifier is
# *structurally* ReDoS-safe: there is no ambiguous alternation, no nested
# quantifier, and no backtracking branch, so matching time is linear in
# the input length regardless of payload (verified by tests with inputs
# of 10**6+ characters in :mod:`tests.test_validators`).
SAFE_IDENTIFIER_PATTERN: str = r"^[A-Za-z0-9_-]+$"

# Drop-in type alias for FastAPI path parameters.  Bundling the pattern
# and both length bounds into a single ``Annotated`` alias means new
# routes get full validation by writing ``strategy_id: SafeIdentifier``
# instead of re-deriving (and possibly diverging from) the contract in
# every module.  ``min_length`` is stated explicitly even though the
# pattern's ``+`` already forbids the empty string, so the boundary is
# enforced identically across the pattern and the length validator and
# shows up in the generated OpenAPI schema.
SafeIdentifier = Annotated[
    str,
    Path(
        pattern=SAFE_IDENTIFIER_PATTERN,
        min_length=MIN_IDENTIFIER_LENGTH,
        max_length=MAX_IDENTIFIER_LENGTH,
    ),
]
