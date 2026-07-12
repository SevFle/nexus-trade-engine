"""Validate docker-compose.yml and .env.example for the core dev stack.

These tests enforce the contract documented in the compose file's header:
the core dev compose file must wire together services — a TimescaleDB-backed
Postgres, Valkey, and the FastAPI application built from the repo Dockerfile —
with health-gated startup ordering and env vars that point the app at the
sibling services by DNS name.

Docker is not required to run these tests: the compose file is parsed
directly with PyYAML (already a project dependency) and each structural
requirement is checked against the parsed document. This mirrors how
``docker compose config`` would interpolate/validate the file, minus the
runtime dependency.

Normalization helpers
---------------------
Docker Compose permits several *equivalent* YAML spellings for the same
field, e.g. ``environment`` may be a mapping **or** a list of ``KEY=value``
strings; ``ports`` entries may be strings, ints, or long-syntax dicts; and
``volumes`` may use the short ``source:target`` string form **or** the
long-syntax dict form. The helpers below normalize every one of those
spellings to a single canonical Python representation so the assertions in
this file are robust to whichever valid form the compose author chose.
They are unit-tested in :class:`TestNormalizationHelpers` so every branch
(int ports, dict ports, list environments, long-syntax volumes, …) is
exercised independently of the real compose file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_FILE = REPO_ROOT / ".env.example"
DOCKERFILE = REPO_ROOT / "Dockerfile"


# --------------------------------------------------------------------------- #
# Normalization helpers (compose field → canonical python form)
# --------------------------------------------------------------------------- #
def normalize_environment(env: Any) -> dict[str, str]:
    """Normalize a compose ``environment`` field to a ``dict[str, str]``.

    Compose accepts two equivalent forms for a service's ``environment``::

        # mapping form
        environment:
          KEY: value
          OTHER: "1"

        # list form
        environment:
          - KEY=value
          - OTHER=1

    Both are normalized to ``{"KEY": "value", "OTHER": "1"}``. A list entry
    without an ``=`` (a bare flag) maps to an empty string, matching how
    Compose injects it (the variable is exported, empty). ``None`` (the field
    was absent) normalizes to an empty dict.
    """
    if env is None:
        return {}
    if isinstance(env, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in env.items()}
    if isinstance(env, list):
        result: dict[str, str] = {}
        for entry in env:
            text = str(entry)
            key, sep, value = text.partition("=")
            result[key] = value if sep else ""
        return result
    raise TypeError(f"environment must be a dict or list, got {type(env).__name__}")


def normalize_ports(ports: Any) -> list[str]:
    """Normalize a compose ``ports`` field to a ``list[str]``.

    Each port entry may be spelled several valid ways::

        ports:
          - "8000:8000"                 # short string
          - 8000                        # bare integer
          - "127.0.0.1:8000:8000"       # host-ip short string
          - target: 8000                # long syntax
            published: "8000"
            protocol: tcp

    Every entry is reduced to a plain string. Ints are stringified directly;
    long-syntax dicts are rebuilt as ``"published:target"`` (or just
    ``"target"`` when ``published`` is absent) so callers can search the
    rendered text for substrings like ``"8000:8000"`` regardless of how the
    author wrote it.
    """
    if ports is None:
        return []
    if isinstance(ports, (str, int)):
        # A single scalar port (unusual but valid YAML).
        return [str(ports)]
    if not isinstance(ports, list):
        raise TypeError(f"ports must be a list, got {type(ports).__name__}")

    rendered: list[str] = []
    for entry in ports:
        if isinstance(entry, dict):
            target = entry.get("target")
            published = entry.get("published")
            if published is not None and target is not None:
                rendered.append(f"{published}:{target}")
            elif target is not None:
                rendered.append(str(target))
            elif published is not None:
                rendered.append(str(published))
            else:
                rendered.append(str(entry))
        else:
            # str, int, float — stringify before joining so int ports
            # (e.g. ``- 8000``) do not raise ``TypeError: sequence item``.
            rendered.append(str(entry))
    return rendered


def normalize_volumes(volumes: Any) -> list[dict[str, str | None]]:
    """Normalize a compose ``volumes`` field to a list of mappings.

    Each entry carries ``source`` and ``target`` keys plus the original raw
    value. Compose supports two forms::

        volumes:
          - pgdata:/var/lib/postgresql/data     # short string
          - ./init.sql:/docker-entrypoint.d:ro  # short string with mode
          - type: volume                         # long syntax
            source: pgdata
            target: /var/lib/postgresql/data

    The short ``source:target[:mode]`` string is split on ``:``; a bare
    ``target`` (anonymous volume) yields ``source=None``. The long-syntax
    dict exposes its ``source``/``target`` keys directly. ``None`` (field
    absent) normalizes to an empty list.
    """
    if volumes is None:
        return []
    if isinstance(volumes, str):
        volumes = [volumes]
    if not isinstance(volumes, list):
        raise TypeError(f"volumes must be a list, got {type(volumes).__name__}")

    normalized: list[dict[str, str | None]] = []
    for entry in volumes:
        if isinstance(entry, dict):
            normalized.append(
                {
                    "source": entry.get("source"),
                    "target": entry.get("target"),
                    "raw": entry,
                }
            )
            continue
        text = str(entry)
        parts = text.split(":")
        if len(parts) >= 2:
            source: str | None = parts[0]
            target = parts[1]
        else:
            source = None
            target = parts[0]
        normalized.append({"source": source, "target": target, "raw": text})
    return normalized


# --------------------------------------------------------------------------- #
# Pytest fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def compose() -> dict:
    """Parsed docker-compose.yml document."""
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    return compose.get("services", {})


@pytest.fixture(scope="module")
def env_example() -> str:
    return ENV_EXAMPLE_FILE.read_text()


# ───────────────────────── normalization helper unit tests ────────────────


class TestNormalizationHelpers:
    """Cover every branch of the three normalize_* helpers independently of
    the real compose file, so the int-port / dict-port / list-environment /
    long-syntax-volume code paths are exercised even when docker-compose.yml
    only ever uses the canonical short-string spellings."""

    def test_environment_none_is_empty_dict(self):
        assert normalize_environment(None) == {}

    def test_environment_dict_form_passes_through(self):
        assert normalize_environment({"A": "1", "B": "x"}) == {"A": "1", "B": "x"}

    def test_environment_dict_none_value_becomes_empty_string(self):
        assert normalize_environment({"A": None}) == {"A": ""}

    def test_environment_list_form_splits_on_equals(self):
        assert normalize_environment(["A=1", "B=two", "FLAG"]) == {
            "A": "1",
            "B": "two",
            "FLAG": "",
        }

    def test_environment_list_value_containing_equals_kept_intact(self):
        # ``partition`` splits only on the first ``=``.
        assert normalize_environment(["URL=postgres://u:p@h:5432/db"]) == {
            "URL": "postgres://u:p@h:5432/db"
        }

    def test_environment_invalid_type_raises(self):
        with pytest.raises(TypeError):
            normalize_environment("not-a-mapping")  # type: ignore[arg-type]

    def test_ports_none_is_empty_list(self):
        assert normalize_ports(None) == []

    def test_ports_string_entries_joined_verbatim(self):
        assert normalize_ports(["127.0.0.1:8000:8000", "9000:9000"]) == [
            "127.0.0.1:8000:8000",
            "9000:9000",
        ]

    def test_ports_integer_entry_stringified_before_join(self):
        # The bug this guards against: ``" ".join([8000])`` raises TypeError.
        assert normalize_ports([8000, "8001:8001"]) == ["8000", "8001:8001"]

    def test_ports_dict_long_syntax_with_published_and_target(self):
        assert normalize_ports(
            [{"published": "8000", "target": 8000, "protocol": "tcp"}]
        ) == ["8000:8000"]

    def test_ports_dict_long_syntax_target_only(self):
        assert normalize_ports([{"target": 5432}]) == ["5432"]

    def test_ports_scalar_int(self):
        assert normalize_ports(8000) == ["8000"]

    def test_ports_invalid_type_raises(self):
        with pytest.raises(TypeError):
            normalize_ports({"target": 1})  # type: ignore[arg-type]

    def test_volumes_none_is_empty_list(self):
        assert normalize_volumes(None) == []

    def test_volumes_short_string_with_source_and_target(self):
        out = normalize_volumes(["pgdata:/var/lib/postgresql/data"])
        assert out == [
            {
                "source": "pgdata",
                "target": "/var/lib/postgresql/data",
                "raw": "pgdata:/var/lib/postgresql/data",
            }
        ]

    def test_volumes_short_string_with_mode_strips_mode(self):
        out = normalize_volumes(["./init.sql:/entrypoint.d/01.sql:ro"])
        assert out[0]["source"] == "./init.sql"
        assert out[0]["target"] == "/entrypoint.d/01.sql"

    def test_volumes_anonymous_target_has_none_source(self):
        out = normalize_volumes(["/var/lib/data"])
        assert out[0]["source"] is None
        assert out[0]["target"] == "/var/lib/data"

    def test_volumes_long_syntax_dict_extracts_source_target(self):
        out = normalize_volumes(
            [{"type": "volume", "source": "pgdata", "target": "/data"}]
        )
        assert out[0]["source"] == "pgdata"
        assert out[0]["target"] == "/data"

    def test_volumes_long_syntax_dict_missing_keys(self):
        out = normalize_volumes([{"type": "tmpfs", "target": "/cache"}])
        assert out[0]["source"] is None
        assert out[0]["target"] == "/cache"


# ───────────────────────── file presence / parse ─────────────────────────


class TestComposeFile:
    def test_compose_file_exists(self):
        assert COMPOSE_FILE.is_file(), f"{COMPOSE_FILE} must exist at repo root"

    def test_compose_parses_as_yaml(self, compose: dict):
        assert isinstance(compose, dict), "docker-compose.yml must parse to a mapping"
        assert "services" in compose and isinstance(compose["services"], dict)

    def test_dockerfile_exists_for_app_build(self):
        # The app service is `build`-ed from ./Dockerfile; make sure that
        # file is actually present so `docker compose up --build` works
        # from a fresh clone.
        assert DOCKERFILE.is_file(), f"{DOCKERFILE} must exist for the app build context"

    def test_env_example_exists(self):
        assert ENV_EXAMPLE_FILE.is_file(), ".env.example must exist at repo root"


# ───────────────────────── service topology ──────────────────────────────


REQUIRED_SERVICE_NICKNAMES = {
    "database": "postgres",
    "cache": "valkey",
    "app": "nexus-api",
}


class TestServiceTopology:
    def test_all_core_services_present(self, services: dict):
        missing = [
            name
            for nickname, name in REQUIRED_SERVICE_NICKNAMES.items()
            if name not in services
        ]
        assert not missing, f"docker-compose.yml missing core services: {missing}"

    def test_has_user_defined_network(self, compose: dict):
        # A user-defined bridge network lets services resolve each other by
        # DNS name (e.g. postgres:5432). The default network does not.
        networks = compose.get("networks", {})
        assert networks, "compose should declare at least one user-defined network"


# ───────────────────────── postgres / TimescaleDB ────────────────────────


class TestPostgresService:
    @property
    def svc(self) -> str:
        return REQUIRED_SERVICE_NICKNAMES["database"]

    def test_uses_timescaledb_image(self, services: dict):
        image = services[self.svc]["image"]
        assert "timescale" in image and "timescaledb" in image, (
            f"db service must use the TimescaleDB image, got {image!r}"
        )
        # Pinned tag (not :latest) so builds are reproducible.
        assert ":" in image and not image.endswith(":latest"), (
            f"db image must be pinned to a specific tag, got {image!r}"
        )

    def test_postgres_credentials_from_env(self, services: dict):
        env = normalize_environment(services[self.svc]["environment"])
        for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            assert var in env, f"db service must set {var}"
            # Should interpolate from .env, not hard-code a literal value.
            assert "${" in env[var], (
                f"{var} should be interpolated from the environment, got {env[var]!r}"
            )

    def test_has_healthcheck(self, services: dict):
        hc = services[self.svc].get("healthcheck")
        assert hc, "db service must define a healthcheck"
        test_cmd = hc.get("test") or []
        joined = " ".join(str(t) for t in test_cmd)
        assert "pg_isready" in joined, (
            "db healthcheck should use pg_isready to verify Postgres readiness"
        )
        # Retry budget large enough for first-boot TimescaleDB init.
        assert hc.get("retries", 0) >= 5

    def test_has_persistent_volume(self, services: dict, compose: dict):
        vols = normalize_volumes(services[self.svc].get("volumes"))
        assert vols, "db service must mount a persistent volume"
        # A named-volume mount has a ``source`` with no path separator (so it
        # refers to a top-level named volume, not a bind mount).
        named = [
            v for v in vols if v["source"] and "/" not in str(v["source"])
        ]
        assert named, "db service must use a named volume for data persistence"
        declared = compose.get("volumes", {})
        for mount in named:
            assert mount["source"] in declared, (
                f"named volume {mount['source']!r} must be declared in top-level volumes:"
            )


# ───────────────────────── valkey / redis ────────────────────────────────


class TestValkeyService:
    @property
    def svc(self) -> str:
        return REQUIRED_SERVICE_NICKNAMES["cache"]

    def test_uses_valkey_image(self, services: dict):
        image = services[self.svc]["image"]
        assert "valkey" in image, f"cache service must use a Valkey image, got {image!r}"
        assert ":" in image and not image.endswith(":latest"), (
            f"valkey image must be pinned to a specific tag, got {image!r}"
        )

    def test_has_healthcheck(self, services: dict):
        hc = services[self.svc].get("healthcheck")
        assert hc, "valkey service must define a healthcheck"
        joined = " ".join(str(t) for t in (hc.get("test") or []))
        assert "ping" in joined, "valkey healthcheck should ping the server to verify readiness"


# ───────────────────────── FastAPI app service ───────────────────────────


class TestAppService:
    @property
    def svc(self) -> str:
        return REQUIRED_SERVICE_NICKNAMES["app"]

    def test_builds_from_repo_dockerfile(self, services: dict):
        build = services[self.svc].get("build")
        assert build, "app service must build from the repo Dockerfile (not pull an image)"
        df = build.get("dockerfile") if isinstance(build, dict) else None
        assert df == "Dockerfile", f"app build must target ./Dockerfile, got {build!r}"

    def test_depends_on_healthy_db_and_valkey(self, services: dict):
        depends = services[self.svc].get("depends_on", {})
        db_name = REQUIRED_SERVICE_NICKNAMES["database"]
        cache_name = REQUIRED_SERVICE_NICKNAMES["cache"]
        for dep in (db_name, cache_name):
            assert dep in depends, f"app must depend_on {dep}"
            assert depends[dep].get("condition") == "service_healthy", (
                f"app depends_on.{dep} must use condition: service_healthy"
            )

    def test_exposes_port_8000(self, services: dict):
        # Ports may be strings, ints, or long-syntax dicts; normalize to
        # strings before searching so any valid spelling is accepted.
        joined = " ".join(normalize_ports(services[self.svc].get("ports")))
        assert "8000:8000" in joined, (
            f"app service must publish port 8000:8000, got {joined!r}"
        )

    def test_env_points_at_compose_services(self, services: dict):
        # ``environment`` may be a mapping or a list of KEY=value strings;
        # normalize to a dict so either form resolves correctly.
        env = normalize_environment(services[self.svc].get("environment"))
        db_url = env.get("NEXUS_DATABASE_URL", "")
        valkey_url = env.get("NEXUS_VALKEY_URL", "")
        db_name = REQUIRED_SERVICE_NICKNAMES["database"]
        cache_name = REQUIRED_SERVICE_NICKNAMES["cache"]
        assert db_url, "app environment must set NEXUS_DATABASE_URL"
        assert valkey_url, "app environment must set NEXUS_VALKEY_URL"
        assert db_name in db_url, (
            f"NEXUS_DATABASE_URL must point at the {db_name} service, got {db_url!r}"
        )
        assert cache_name in valkey_url, (
            f"NEXUS_VALKEY_URL must point at the {cache_name} service, got {valkey_url!r}"
        )

    def test_has_healthcheck(self, services: dict):
        # The distroless runtime image has no shell/curl, so the healthcheck
        # must invoke python to hit the liveness route.
        hc = services[self.svc].get("healthcheck")
        assert hc, "app service must define a healthcheck"
        joined = " ".join(str(t) for t in (hc.get("test") or []))
        assert "/health" in joined or "urllib" in joined, (
            "app healthcheck should probe the liveness endpoint"
        )


# ───────────────────────── .env.example contract ─────────────────────────


class TestEnvExample:
    def test_contains_database_url(self, env_example: str):
        assert "NEXUS_DATABASE_URL" in env_example, ".env.example must document NEXUS_DATABASE_URL"

    def test_contains_valkey_url(self, env_example: str):
        assert "NEXUS_VALKEY_URL" in env_example, ".env.example must document NEXUS_VALKEY_URL"

    def test_contains_postgres_bootstrap_vars(self, env_example: str):
        # The compose db service interpolates these from .env.
        for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            assert var in env_example, f".env.example must document {var}"

    @pytest.mark.parametrize(
        "key",
        [
            "NEXUS_APP_ENV",
            "NEXUS_SECRET_KEY",
            "NEXUS_LOG_LEVEL",
            "NEXUS_CORS_ORIGINS",
        ],
    )
    def test_contains_core_config_keys(self, env_example: str, key: str):
        assert key in env_example, f".env.example must document {key}"


# ───────────────────────── env-var naming alignment ──────────────────────


class TestEnvNamingAlignment:
    """Guarantee the env vars referenced by docker-compose.yml match the
    NEXUS_-prefixed settings declared in engine/config.py (env_prefix
    "NEXUS_"), so the containerized app actually consumes the wiring."""

    def test_app_env_uses_nexus_prefix(self, services: dict):
        env = normalize_environment(
            services[REQUIRED_SERVICE_NICKNAMES["app"]].get("environment")
        )
        nexus_keys = [k for k in env if k.startswith("NEXUS_")]
        assert nexus_keys, (
            "app environment must set at least one NEXUS_-prefixed var "
            "(matching engine/config.py env_prefix)"
        )
        assert "NEXUS_DATABASE_URL" in nexus_keys
        assert "NEXUS_VALKEY_URL" in nexus_keys
