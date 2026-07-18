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

Every environment lookup uses the ``os.environ.get("KEY") or "default"``
form rather than ``os.environ.get("KEY", "default")``. The difference
matters: a variable that is *present but empty* (``POSTGRES_PORT=""``) is
returned verbatim by the two-argument form, silently overriding the
default with an empty string; the ``or`` form treats an empty value as
"unset" and falls back to the default, which is the only sane behaviour
for a value that is about to be spliced into a URL.

``POSTGRES_PORT`` / ``VALKEY_PORT`` are additionally run through
:func:`validate_port`, which parses them as integers and rejects anything
outside the valid TCP range ``[1, 65535]`` (an out-of-range port would
otherwise be encoded into the URL and only blow up later as an opaque
``connection refused`` / ``address in use`` at connect time).

``VALKEY_DB`` is validated as a non-negative integer by
:func:`validate_valkey_db`; values above ``15`` (the maximum logical
database index exposed by a default Redis / Valkey build) emit a
:class:`UserWarning` so operators learn immediately that the instance
must be started with ``--databases N`` or the connection will be rejected.

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
    POSTGRES_PORT       5432       (validated by :func:`validate_port`)
    VALKEY_PASSWORD     optional; when set the URL becomes valkey://:pw@…
    VALKEY_HOST         valkey
    VALKEY_PORT         6379       (validated by :func:`validate_port`)
    VALKEY_DB           0          (logical database index, validated as a
                        non-negative integer; >15 warns the operator)

Usage in docker-compose.yml::

    entrypoint: ["python", "/app/scripts/compose-entrypoint.py"]
    command:    ["uvicorn", "engine.app:create_app", "--factory", ...]
"""

from __future__ import annotations

import os
import sys
import warnings
from urllib.parse import quote

__all__ = [
    "MAX_PORT",
    "MAX_VALKEY_DB",
    "MIN_PORT",
    "build_database_url",
    "build_valkey_url",
    "main",
    "validate_port",
    "validate_valkey_db",
]

# Minimum argv length: the script name plus the real command to exec.
_MIN_ARGV_LEN = 2

# Valid TCP/UDP port range: ``1``..``65535``. ``0`` is reserved and values
# above ``65535`` do not fit in a 16-bit port field, so reject either edge at
# startup rather than splicing an out-of-range port into the connection URL.
MIN_PORT = 1
MAX_PORT = 65535

# Redis / Valkey expose logical databases ``0``..``N-1`` where ``N`` defaults to
# 16 (i.e. valid indices ``0``..``15``). Anything above this is only usable if
# the server was started with ``--databases N`` / ``databases N`` in its config;
# warn rather than reject so a deliberately reconfigured cluster still boots.
MAX_VALKEY_DB = 15


def build_database_url(user: str, password: str, db: str, host: str, port: str) -> str:
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
        f"postgresql+asyncpg://{quote(user, safe='')}:"
        f"{quote(password, safe='')}@{quote(host, safe='')}:"
        f"{quote(port, safe='')}/{quote(db, safe='')}"
    )


def build_valkey_url(host: str, port: str, password: str = "", db: str = "0") -> str:
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
            f"valkey://:{quote(password, safe='')}@{encoded_host}:"
            f"{encoded_port}/{encoded_db}"
        )
    return f"valkey://{encoded_host}:{encoded_port}/{encoded_db}"


def validate_port(raw: str) -> str:
    """Validate a TCP port read from the environment.

    ``POSTGRES_PORT`` / ``VALKEY_PORT`` are read verbatim from the
    environment and spliced into a URL, so a typo (``POSTGRES_PORT=54a32``),
    an empty string, a negative number or a value outside the valid TCP
    range would otherwise be encoded silently and only surface as an opaque
    ``connection refused`` / ``address not available`` when the driver tries
    to connect. Reject anything that is not an integer in ``[1, 65535]`` so
    the container fails loudly at startup instead.

    :returns: the validated port as a plain digit string, ready to splice
        into the URL (so leading zeros / surrounding whitespace can never
        reach the connection string).
    :raises ValueError: if ``raw`` is not an integer in ``[1, 65535]``.
    """
    value = (raw or "").strip()
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"port must be an integer between {MIN_PORT} and {MAX_PORT}, got {raw!r}"
        ) from exc
    if port < MIN_PORT or port > MAX_PORT:
        raise ValueError(
            f"port must be an integer between {MIN_PORT} and {MAX_PORT}, got {raw!r}"
        )
    return str(port)


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

    A default Redis / Valkey build only exposes databases ``0``..``15`` (the
    ``databases`` config defaults to ``16``). A value above
    :data:`MAX_VALKEY_DB` is therefore almost always a mistake and would be
    rejected by the server at connect time; emit a :class:`UserWarning`
    (rather than raising) so a cluster deliberately reconfigured with a
    larger ``--databases N`` still boots, while operators running a default
    build are warned before the connection silently fails.

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
            "VALKEY_DB must be a non-negative integer, got " + repr(raw)
        ) from exc
    if index < 0:
        raise ValueError(
            "VALKEY_DB must be a non-negative integer, got " + repr(raw)
        )
    if index > MAX_VALKEY_DB:
        warnings.warn(
            "VALKEY_DB="
            + str(index)
            + " exceeds the default Redis/Valkey maximum of "
            + str(MAX_VALKEY_DB)
            + " logical databases; an unconfigured instance only exposes "
            "indices 0-"
            + str(MAX_VALKEY_DB)
            + " and will reject this index at connect time. Start the "
            "server with '--databases N' (or lower VALKEY_DB) if this is "
            "intentional.",
            UserWarning,
            stacklevel=2,
        )
    return str(index)


def main() -> None:
    """Read env vars, set the encoded connection URLs, exec the command."""
    # ``or ""`` keeps an explicitly-empty value empty; the not-password check
    # below rejects it before any URL is assembled.
    password = os.environ.get("POSTGRES_PASSWORD") or ""
    if not password:
        sys.stderr.write(
            "compose-entrypoint: POSTGRES_PASSWORD is required and must be non-empty\n"
        )
        raise SystemExit(2)

    # Validate the two ports up front so a malformed value fails loudly here
    # instead of being encoded into a URL that only blows up on connect.
    try:
        postgres_port = validate_port(os.environ.get("POSTGRES_PORT") or "5432")
    except ValueError as exc:
        sys.stderr.write(f"compose-entrypoint: {exc}\n")
        raise SystemExit(2) from exc

    try:
        valkey_port = validate_port(os.environ.get("VALKEY_PORT") or "6379")
    except ValueError as exc:
        sys.stderr.write(f"compose-entrypoint: {exc}\n")
        raise SystemExit(2) from exc

    try:
        valkey_db = validate_valkey_db(os.environ.get("VALKEY_DB") or "0")
    except ValueError as exc:
        sys.stderr.write(f"compose-entrypoint: {exc}\n")
        raise SystemExit(2) from exc

    db_url = build_database_url(
        user=os.environ.get("POSTGRES_USER") or "nexus",
        password=password,
        db=os.environ.get("POSTGRES_DB") or "nexus",
        host=os.environ.get("POSTGRES_HOST") or "postgres",
        port=postgres_port,
    )
    valkey_url = build_valkey_url(
        host=os.environ.get("VALKEY_HOST") or "valkey",
        port=valkey_port,
        password=os.environ.get("VALKEY_PASSWORD") or "",
        db=valkey_db,
    )

    os.environ["NEXUS_DATABASE_URL"] = db_url
    os.environ["NEXUS_VALKEY_URL"] = valkey_url

    if len(sys.argv) < _MIN_ARGV_LEN:
        sys.stderr.write(
            "compose-entrypoint: missing command to exec (expected: "
            "compose-entrypoint.py <real-cmd> [args...])\n"
        )
        raise SystemExit(2)

    # Replace this wrapper process (PID 1) with the real entrypoint so the
    # service keeps PID 1 and correct signal handling. The argv is taken
    # verbatim from the operator-controlled compose ``command:`` list, not
    # from untrusted input, so ``execvp`` without a shell is intentional and
    # safe here.
    os.execvp(sys.argv[1], sys.argv[1:])  # noqa: S606 - intentional, see above


if __name__ == "__main__":
    main()
