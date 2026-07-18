"""Shared FastAPI validators for user-controlled identifier parameters.

Strategy, scoring, and plugin identifiers flow from ``Path`` / ``Query``
parameters into registry lookups, DB queries, log lines, and reflected
error ``detail`` strings.  Centralizing the validation pattern here means
every route module enforces the *same* contract — there is no risk of one
route drifting to a looser or stricter regex than another — and the
contract is unit-testable in isolation without standing up a router.

Contract for a "safe identifier"
--------------------------------
* Drawn from ``[A-Za-z0-9_-]`` plus ``.`` as a *separator* (dots are only
  legal between non-empty, dot-free tokens).
* Non-empty (the leading ``[A-Za-z0-9_-]+`` token requires at least one
  character).
* No leading or trailing dot, and no consecutive dots (``..``) — these
  fall out of the "token (``.`` token)*" formulation rather than being
  forbidden by look-around, so a dot can never open/close an identifier
  or be mistaken for a relative-path component (``./name`` / ``name.`` /
  ``a..b``).
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
:class:`fastapi.Path` marker, and enforces both the pattern and the length
cap at the validation layer — returning HTTP 422 *before* the handler
runs for any non-conforming value.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

# Upper bound enforced on every identifier.  Mirrored here as a named
# constant (rather than inlined as the literal ``64`` in :data:`SafeIdentifier`)
# so tests and callers can reference the same value and stay in sync if the
# cap ever changes.
MAX_IDENTIFIER_LENGTH: int = 64

# Shared, anchored pattern.  The ``^...$`` anchors make the contract
# independent of whether the validator uses ``re.match``, ``re.search``, or
# ``re.fullmatch`` (pydantic/FastAPI versions differ).
#
# Dot discipline is encoded *without* look-around assertions because
# pydantic v2 compiles the pattern with the Rust ``regex`` crate, which
# does not support lookahead/look-behind (a look-around pattern raises
# ``regex parse error`` at app import time).  The formulation "one or more
# dot-free tokens separated by *single* dots" achieves the same contract
# constructively:
#
#   [A-Za-z0-9_-]+           — first token: ≥1 dot-free char, so the string
#                              can neither be empty nor *start* with a dot
#   (\.[A-Za-z0-9_-]+)*      — zero or more ``.`` separators, each of which
#                              MUST be followed by ≥1 dot-free char, so a
#                              trailing dot, a leading dot in a later
#                              segment, and consecutive dots (``..``) are
#                              all structurally impossible
#
# Dots are therefore allowed *only* as single separators between non-empty
# dot-free tokens — exactly the "allow dots but deny ``..`` and
# leading/trailing dots" contract, expressed in plain regex.
SAFE_IDENTIFIER_PATTERN: str = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$"

# Drop-in type alias for FastAPI path parameters.  Bundling the pattern and
# the length cap into a single ``Annotated`` alias means new routes get full
# validation by writing ``strategy_id: SafeIdentifier`` instead of
# re-deriving (and possibly diverging from) the pattern in every module.
SafeIdentifier = Annotated[
    str,
    Path(
        pattern=SAFE_IDENTIFIER_PATTERN,
        max_length=MAX_IDENTIFIER_LENGTH,
    ),
]
