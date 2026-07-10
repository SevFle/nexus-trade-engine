#!/usr/bin/env python3
"""Compose entrypoint wrapper: URL-encode DB / Valkey passwords.

The Nexus API and the TaskIQ worker each consume two connection strings:

    NEXUS_DATABASE_URL   postgresql+asyncpg://user:pw@host:port/db
    NEXUS_VALKEY_URL     valkey://[:pw@]host:port/db

Docker Compose interpolates ``${POSTGRES_PASSWORD}`` / ``${VALKEY_PASSWORD}``
verbatim — it cannot URL-encode them — so a password containing reserved
URI characters (``@``, ``/``, ``:``, ``%``, ``#``, ``+`` …) would silently
corrupt the URL and the service would fail to connect with an opaque
``invalid URL`` / ``authentication failed`` error that gives no hint the
password is the culprit.

This wrapper rebuilds both URLs from their individual ``POSTGRES_*`` /
``VALKEY_*`` parts, percent-encoding *every* path / authority component
with :func:`urllib.parse.quote` (the encoder SQLAlchemy / asyncpg /
redis-py expect in a connection URL), then ``exec``s the real entrypoint
(``uvicorn`` / ``taskiq``) so the process keeps PID 1 and signal handling
stays correct.

Every value taken straight from the environment — ``host``, ``port``,
``db`` and the password — is passed through ``quote(..., safe="")`` so a
reserved character in *any* component is encoded rather than corrupting the
URL. ``quote`` leaves ordinary DNS labels / digit ports / alnum DB names
untouched, so the happy-path compose wiring is byte-identical to the
unquoted build.

The URL builders are exposed as pure functions (:func:`build_database_url`
and :func:`build_valkey_url`) so they can be unit-tested directly without
spawning a process. All side effects (writing ``os.environ`` and the
``exec``) live in :func:`main`, which only runs when this file is invoked
as a script — importing the module is therefore side-effect free.

Environment consumed (defaults shown):

    POSTGRES_USER       nexus
    POSTGRES_PASSWORD   REQUIRED (raw, un-encoded password); empty/missing
                        is rejected with ``SystemExit(2)`` before any URL
                        is assembled (fail-fast — a passwordless URL would
                        either be rejected by asyncpg with an opaque error
                        or, worse, silently authenticate as a no-password
                        role).
    POSTGRES_DB         nexus
    POSTGRES_HOST       postgres   (compose service DNS name)
    POSTGRES_PORT       5432
    VALKEY_PASSWORD     optional; when set the URL becomes valkey://:pw@…
    VALKEY_HOST         valkey
    VALKEY_PORT         6379
    VALKEY_DB           0          (logical database index, validated as a
                        non-negative integer and appended as /<db>)

Usage in docker-compose.yml::

    entrypoint: ["python", "/app/scripts/compose-entrypoint.py"]
    command:    ["uvicorn", "engine.app:create_app", "--factory", ...]
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

# Minimum argv length we require before exec'ing: argv[0] is the wrapper
# script name and argv[1] is the real command (``uvicorn`` / ``taskiq``).
_MIN_ARGV_LEN = 2


def build_database_url(
    *, user: str, password: str, db: str, host: str, port: str
) -> str:
    """Assemble a postgres URL with the password percent-encoded.

    ``safe=""`` so *every* reserved character (``@``, ``/``, ``:``, ``%`` …)
    is encoded in *every* component — user, password, host, port and the
    database name — so asyncpg / SQLAlchemy then decode each component
    before authenticating and the original value round-trips exactly.

    Encoding the ``db`` component matters because it is read verbatim from
    ``POSTGRES_DB``: an operator value carrying a ``/`` or ``?`` would
    otherwise be mis-parsed as an extra path segment / query string and the
    service would connect to (or fail to find) the wrong database.
    """
    return (
        "postgresql+asyncpg://"
        f"{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{quote(host, safe='')}:{quote(port, safe='')}/{quote(db, safe='')}"
    )


def build_valkey_url(
    *, host: str, port: str, password: str = "", db: str = "0"
) -> str:
    """Assemble a valkey URL, embedding the password when one is set.

    Redis / Valkey URLs carry the password in the userinfo with an *empty*
    username (``valkey://:pw@host``). When no password is set the URL is
    built without userinfo so an unauthenticated dev instance keeps working.

    ``db`` is the logical database index appended as ``/<db>`` (defaults to
    ``"0"``) and is taken from the ``VALKEY_DB`` environment variable by
    :func:`main`, so deployments can target a non-default logical DB without
    hand-editing the connection string.

    As with :func:`build_database_url`, ``host``, ``port`` and ``db`` are
    passed through ``quote(..., safe="")``: although a well-formed DNS name
    / integer port / DB index contains no reserved characters, the values
    come straight from the environment and an operator-supplied value
    carrying a space, ``@``, ``/`` or ``:`` would otherwise corrupt the
    authority or path section of the URL. ``quote`` leaves digits and labels
    untouched, so a normal ``valkey:6379/0`` is unchanged.
    """
    encoded_host = quote(host, safe="")
    encoded_port = quote(port, safe="")
    encoded_db = quote(db, safe="")
    if password:
        return (
            f"valkey://:{quote(password, safe='')}"
            f"@{encoded_host}:{encoded_port}/{encoded_db}"
        )
    return f"valkey://{encoded_host}:{encoded_port}/{encoded_db}"


def validate_valkey_db(raw: str) -> str:
    """Validate the ``VALKEY_DB`` logical database index.

    Redis / Valkey URLs carry a 0-based logical database index in the path
    (``valkey://host:port/<db>``). The value comes straight from the
    environment, so a typo (``VALKEY_DB=db0``), an empty string, or a hostile
    value carrying ``/`` / ``?`` / ``#`` / spaces would either be mis-parsed
    or silently corrupt the URL (e.g. ``/`` in the index would create an
    extra path segment, ``#`` would start a fragment). Reject anything that
    isn't a non-negative integer so the container fails loudly at startup
    instead of connecting to the wrong logical DB.

    :returns: the validated index as a plain digit string, ready to splice
        into the URL (so leading zeros / surrounding whitespace can never
        reach the connection string).
    :raises ValueError: if ``raw`` is not a non-negative integer.
    """
    value = (raw or "").strip()
    try:
        index = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"VALKEY_DB must be a non-negative integer, got {raw!r}"
        ) from exc
    if index < 0:
        raise ValueError(
            f"VALKEY_DB must be a non-negative integer, got {raw!r}"
        )
    # Re-stringify so leading zeros / whitespace never reach the URL and the
    # value round-trips through build_valkey_url unchanged.
    return str(index)


def main() -> None:
    """Read env vars, set the encoded connection URLs, exec the command."""
    # Fail fast: a missing/empty POSTGRES_PASSWORD is a deployment error.
    # Letting it through would build a URL with empty userinfo that asyncpg
    # rejects with an opaque error — or, worse, authenticate as a
    # passwordless role. Surface the real cause and exit non-zero so the
    # container restarts loudly instead of flapping.
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        sys.stderr.write(
            "compose-entrypoint: POSTGRES_PASSWORD is required and must be "
            "non-empty\n"
        )
        raise SystemExit(2)

    # Validate VALKEY_DB before assembling the URL: a non-integer index would
    # otherwise be spliced raw into the path and could corrupt the URL. Fail
    # fast with the same loud exit code as the password guard.
    try:
        valkey_db = validate_valkey_db(os.environ.get("VALKEY_DB", "0"))
    except ValueError as exc:
        sys.stderr.write(f"compose-entrypoint: {exc}\n")
        raise SystemExit(2) from exc

    db_url = build_database_url(
        user=os.environ.get("POSTGRES_USER", "nexus"),
        password=password,
        db=os.environ.get("POSTGRES_DB", "nexus"),
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
    )
    valkey_url = build_valkey_url(
        host=os.environ.get("VALKEY_HOST", "valkey"),
        port=os.environ.get("VALKEY_PORT", "6379"),
        password=os.environ.get("VALKEY_PASSWORD", ""),
        db=valkey_db,
    )
    # Always (re)set — inside compose the wrapper is the authority, so a
    # stale NEXUS_*_URL carried in from ``env_file`` cannot win over the
    # correctly-encoded, compose-internal DNS name.
    os.environ["NEXUS_DATABASE_URL"] = db_url
    os.environ["NEXUS_VALKEY_URL"] = valkey_url

    if len(sys.argv) < _MIN_ARGV_LEN:
        sys.stderr.write(
            "compose-entrypoint: missing command to exec "
            "(expected: compose-entrypoint.py <real-cmd> [args...])\n"
        )
        raise SystemExit(2)

    # Replace this process with the real entrypoint (uvicorn / taskiq) so
    # it becomes PID 1 and receives signals (docker stop → SIGTERM) directly.
    # S606 ("starting a process without a shell") is intentional: the command
    # comes from the trusted compose ``command:`` field and a shell would
    # fork a child, breaking PID-1 signal handling.
    os.execvp(sys.argv[1], sys.argv[1:])  # noqa: S606


if __name__ == "__main__":  # pragma: no cover - exercised via compose
    main()
