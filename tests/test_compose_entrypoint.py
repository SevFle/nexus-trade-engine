"""Unit tests for ``scripts/compose-entrypoint.py``.

The wrapper file is named with a hyphen (``compose-entrypoint.py``) so it is
not importable as a regular package; these tests load it via :mod:`importlib`
from its on-disk path and exercise the pure URL builders plus the env-driven
``main`` wrapper directly.

The behaviours under test map to the hardening fixes:

1. **Empty password rejection** — ``main`` exits ``2`` when
   ``POSTGRES_PASSWORD`` is missing/empty instead of building a URL with
   empty userinfo.
2. **Consistent component encoding** — :func:`build_database_url` and
   :func:`build_valkey_url` percent-encode *every* component taken from the
   environment (user, password, host, port, db) so a reserved character in
   any of them cannot corrupt the URL.
3. **VALKEY_DB integer validation** — :func:`validate_valkey_db` (and thus
   ``main``) reject anything that isn't a non-negative integer so a hostile
   value carrying ``/`` / ``?`` cannot be spliced into the URL path.
4. **Configurable Valkey DB** — :func:`build_valkey_url` honours a ``db``
   argument (read from ``VALKEY_DB`` by ``main``) instead of hard-coding
   ``/0``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

_ENTRYPOINT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "compose-entrypoint.py"
)


@pytest.fixture(scope="module")
def entrypoint() -> ModuleType:
    """Load compose-entrypoint.py as a module.

    ``__name__`` becomes ``nexus_compose_entrypoint`` (not ``__main__``) so the
    ``if __name__ == "__main__"`` guard keeps ``main`` from running on import —
    importing the module is side-effect free, exactly as designed.
    """
    spec = importlib.util.spec_from_file_location(
        "nexus_compose_entrypoint", _ENTRYPOINT_PATH
    )
    assert spec is not None, f"could not build spec for {_ENTRYPOINT_PATH}"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_env(entrypoint: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run ``main`` against a private copy of ``os.environ``.

    ``main`` writes ``NEXUS_DATABASE_URL`` / ``NEXUS_VALKEY_URL`` directly into
    ``os.environ``; swapping the os module's ``environ`` for a throwaway dict
    means those writes can never leak into the real process environment and
    perturb other tests (e.g. the cached ``engine.config`` settings).

    ``POSTGRES_PASSWORD`` is dropped from the copy so every test starts from
    the same known "unset" state and must opt in explicitly.
    """
    env_copy = dict(os.environ)
    env_copy.pop("POSTGRES_PASSWORD", None)
    env_copy.pop("VALKEY_DB", None)
    monkeypatch.setattr(entrypoint.os, "environ", env_copy)
    return env_copy


def _stub_execvp(
    entrypoint: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """Replace ``os.execvp`` with a recorder that raises a sentinel exit.

    Lets a happy-path ``main`` call reach and invoke ``execvp`` without
    actually replacing the test process.
    """
    called: dict[str, object] = {}

    def fake_execvp(file: str, args: list[str]) -> None:
        called["file"] = file
        called["args"] = list(args)
        raise SystemExit(99)  # sentinel: proves main() reached execvp

    monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
    return called


# ── Fix 1: empty POSTGRES_PASSWORD must fail fast ──────────────────────────


def test_main_rejects_missing_postgres_password(
    entrypoint: ModuleType, isolated_env: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    # isolated_env already drops POSTGRES_PASSWORD.
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2
    assert "POSTGRES_PASSWORD" in capsys.readouterr().err
    # No URL should have been written before the guard fired.
    assert "NEXUS_DATABASE_URL" not in isolated_env


def test_main_rejects_empty_postgres_password(
    entrypoint: ModuleType, isolated_env: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated_env["POSTGRES_PASSWORD"] = ""
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2
    assert "POSTGRES_PASSWORD" in capsys.readouterr().err


def test_main_accepts_nonempty_postgres_password(
    entrypoint: ModuleType, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_env["POSTGRES_PASSWORD"] = "s3cr3t"
    monkeypatch.setattr(sys, "argv", ["compose-entrypoint.py", "uvicorn"])
    called = _stub_execvp(entrypoint, monkeypatch)

    with pytest.raises(SystemExit) as exc:  # sentinel from the execvp stub
        entrypoint.main()
    assert exc.value.code == 99
    # main reached execvp with the real command (PID-1 hand-off intact).
    assert called["file"] == "uvicorn"
    # The password round-trips into the assembled database URL.
    assert "s3cr3t" in isolated_env["NEXUS_DATABASE_URL"]


# ── Fix 2a: build_database_url encodes every component ────────────────────


def test_build_database_url_quotes_host(entrypoint: ModuleType) -> None:
    url = entrypoint.build_database_url(
        user="nexus", password="pw", db="app", host="po@st ex", port="5432"
    )
    # A host containing '@' / space must be encoded so it cannot be mistaken
    # for the userinfo separator.
    assert quote("po@st ex", safe="") in url
    assert "po@st ex" not in url
    assert url.startswith("postgresql+asyncpg://")


def test_build_database_url_quotes_port(entrypoint: ModuleType) -> None:
    url = entrypoint.build_database_url(
        user="nexus", password="pw", db="app", host="postgres", port="54 32"
    )
    # A port containing a space must be encoded too.
    assert quote("54 32", safe="") in url
    assert "54 32" not in url


def test_build_database_url_quotes_db(entrypoint: ModuleType) -> None:
    # A DB name carrying reserved path/query characters must be encoded so it
    # is not mis-parsed as an extra path segment or a query string.
    url = entrypoint.build_database_url(
        user="nexus", password="pw", db="ap/p?x=1", host="postgres", port="5432"
    )
    assert quote("ap/p?x=1", safe="") in url
    # The raw malicious value must not appear verbatim anywhere.
    assert "ap/p?x=1" not in url
    # The only literal '/' characters are the '://' pair plus the single
    # '/<db>' separator — the '/' inside the malicious db was encoded to %2F.
    assert url.count("/") == 3


def test_build_database_url_normal_host_port_unchanged(entrypoint: ModuleType) -> None:
    # quote() leaves ordinary DNS labels / digit ports / alnum DB names
    # untouched, so the happy-path compose wiring is byte-identical to the
    # unquoted build.
    url = entrypoint.build_database_url(
        user="nexus", password="pw", db="nexus", host="postgres", port="5432"
    )
    assert url == "postgresql+asyncpg://nexus:pw@postgres:5432/nexus"


def test_build_database_url_still_quotes_password(entrypoint: ModuleType) -> None:
    # Regression guard: the original password-encoding behaviour is preserved.
    url = entrypoint.build_database_url(
        user="u", password="p@ss/w:rd", db="d", host="h", port="5432"
    )
    assert quote("p@ss/w:rd", safe="") in url
    assert "p@ss/w:rd" not in url


# ── Fix 2b: build_valkey_url encodes every component ──────────────────────


def test_build_valkey_url_quotes_host(entrypoint: ModuleType) -> None:
    url = entrypoint.build_valkey_url(host="va@l key", port="6379")
    assert quote("va@l key", safe="") in url
    # The raw malicious host (with the userinfo separator) must not leak.
    assert "va@l key" not in url


def test_build_valkey_url_quotes_port(entrypoint: ModuleType) -> None:
    url = entrypoint.build_valkey_url(host="valkey", port="63 79")
    assert quote("63 79", safe="") in url
    assert "63 79" not in url


def test_build_valkey_url_quotes_db(entrypoint: ModuleType) -> None:
    # A DB index taken raw from the environment that carries path/query
    # characters must be encoded rather than spliced into the URL path.
    url = entrypoint.build_valkey_url(host="valkey", port="6379", db="0/p?x=1")
    assert quote("0/p?x=1", safe="") in url
    assert "0/p?x=1" not in url
    # The only literal '/' characters are the '://' pair plus the single
    # '/<db>' separator — the '/' inside the malicious db was encoded to %2F.
    assert url.count("/") == 3


def test_build_valkey_url_quotes_host_port_db_with_password(
    entrypoint: ModuleType,
) -> None:
    # All four components (host/port/db + password) are encoded together.
    url = entrypoint.build_valkey_url(
        host="ho@st", port="63 79", password="se@cre:t", db="1/x"
    )
    assert quote("ho@st", safe="") in url
    assert quote("63 79", safe="") in url
    assert quote("se@cre:t", safe="") in url
    assert quote("1/x", safe="") in url
    # None of the raw values survive into the assembled URL.
    for raw in ("ho@st", "63 79", "se@cre:t", "1/x"):
        assert raw not in url


# ── Fix 3: VALKEY_DB integer validation ────────────────────────────────────


@pytest.mark.parametrize("raw", ["db0", "1.5", "", "abc", "0x1", "1-2", "-1", "3/4"])
def test_validate_valkey_db_rejects_non_integer(entrypoint: ModuleType, raw: str) -> None:
    with pytest.raises(ValueError):
        entrypoint.validate_valkey_db(raw)


@pytest.mark.parametrize(("raw", "expected"), [("0", "0"), ("7", "7"), ("  3 ", "3")])
def test_validate_valkey_db_accepts_non_negative_integer(
    entrypoint: ModuleType, raw: str, expected: str
) -> None:
    assert entrypoint.validate_valkey_db(raw) == expected


def test_main_rejects_invalid_valkey_db(
    entrypoint: ModuleType,
    isolated_env: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    isolated_env["POSTGRES_PASSWORD"] = "x"
    isolated_env["VALKEY_DB"] = "db0"
    monkeypatch.setattr(sys, "argv", ["compose-entrypoint.py", "uvicorn"])

    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2
    assert "VALKEY_DB" in capsys.readouterr().err
    # Nothing was written before the guard fired.
    assert "NEXUS_VALKEY_URL" not in isolated_env
    assert "NEXUS_DATABASE_URL" not in isolated_env


def test_main_rejects_hostile_valkey_db(
    entrypoint: ModuleType,
    isolated_env: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hostile index carrying a path separator must not reach the URL.
    isolated_env["POSTGRES_PASSWORD"] = "x"
    isolated_env["VALKEY_DB"] = "0/../../etc"
    monkeypatch.setattr(sys, "argv", ["compose-entrypoint.py", "uvicorn"])

    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2


# ── Fix 4: configurable Valkey DB via build_valkey_url / VALKEY_DB ─────────


def test_build_valkey_url_default_db(entrypoint: ModuleType) -> None:
    # Without an explicit db the logical DB defaults to 0 (back-compat).
    assert entrypoint.build_valkey_url(host="valkey", port="6379").endswith("/0")


def test_build_valkey_url_configurable_db(entrypoint: ModuleType) -> None:
    url = entrypoint.build_valkey_url(host="valkey", port="6379", db="7")
    assert url == "valkey://valkey:6379/7"
    assert url.endswith("/7")


def test_build_valkey_url_db_with_password(entrypoint: ModuleType) -> None:
    url = entrypoint.build_valkey_url(
        host="valkey", port="6379", password="sec", db="3"
    )
    assert url == "valkey://:sec@valkey:6379/3"


def test_main_reads_valkey_db_env(
    entrypoint: ModuleType, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_env["POSTGRES_PASSWORD"] = "x"
    isolated_env["VALKEY_DB"] = "5"
    monkeypatch.setattr(sys, "argv", ["compose-entrypoint.py", "uvicorn"])
    _stub_execvp(entrypoint, monkeypatch)

    with pytest.raises(SystemExit):  # sentinel from the execvp stub
        entrypoint.main()
    assert isolated_env["NEXUS_VALKEY_URL"].endswith("/5")


def test_main_defaults_valkey_db_to_zero(
    entrypoint: ModuleType, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_env["POSTGRES_PASSWORD"] = "x"
    isolated_env.pop("VALKEY_DB", None)
    monkeypatch.setattr(sys, "argv", ["compose-entrypoint.py", "uvicorn"])
    _stub_execvp(entrypoint, monkeypatch)

    with pytest.raises(SystemExit):  # sentinel from the execvp stub
        entrypoint.main()
    assert isolated_env["NEXUS_VALKEY_URL"].endswith("/0")


# ── Regression: argv validation still fires once the password is valid ─────


def test_main_requires_a_command_to_exec(
    entrypoint: ModuleType,
    isolated_env: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    isolated_env["POSTGRES_PASSWORD"] = "x"
    monkeypatch.setattr(sys, "argv", ["compose-entrypoint.py"])  # no real command
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2
    assert "missing command to exec" in capsys.readouterr().err
