"""Validate docker-compose.yml and .env.example for the core dev stack.

These tests enforce the contract documented in the task: the core dev
compose file must wire together three services — a TimescaleDB-backed
Postgres, Valkey, and the FastAPI application built from the repo
Dockerfile — with health-gated startup ordering and env vars that point
the app at the sibling services by DNS name.

Docker is not required to run these tests: the compose file is parsed
directly with PyYAML (already a project dependency) and each structural
requirement is checked against the parsed document. This mirrors how
``docker compose config`` would interpolate/validate the file, minus the
runtime dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_FILE = REPO_ROOT / ".env.example"
DOCKERFILE = REPO_ROOT / "Dockerfile"


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
            name for nickname, name in REQUIRED_SERVICE_NICKNAMES.items() if name not in services
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
        # Pinned tag (not :latest) so builds are reproducible — a prior
        # review explicitly required pinning image tags.
        assert ":" in image and not image.endswith(":latest"), (
            f"db image must be pinned to a specific tag, got {image!r}"
        )

    def test_postgres_credentials_from_env(self, services: dict):
        env = services[self.svc]["environment"]
        for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            assert var in env, f"db service must set {var}"
            # Should interpolate from .env, not hard-code a value.
            assert f"${{{var}" in env[var] or "${" in env[var], (
                f"{var} should be interpolated from the environment, got {env[var]!r}"
            )

    def test_has_healthcheck(self, services: dict):
        hc = services[self.svc].get("healthcheck")
        assert hc, "db service must define a healthcheck"
        test_cmd = hc.get("test") or []
        assert isinstance(test_cmd, list)
        joined = " ".join(str(t) for t in test_cmd)
        assert "pg_isready" in joined, (
            "db healthcheck should use pg_isready to verify Postgres readiness"
        )
        # Retry budget large enough for first-boot TimescaleDB init.
        assert hc.get("retries", 0) >= 5

    def test_has_persistent_volume(self, services: dict, compose: dict):
        volumes = services[self.svc].get("volumes") or []
        assert volumes, "db service must mount a persistent volume"
        # At least one named-volume mount (host:container where host has no
        # path separator → named volume defined in top-level `volumes:`).
        named_mounts = [
            v for v in volumes if isinstance(v, str) and ":" in v and "/" not in v.split(":")[0]
        ]
        assert named_mounts, "db service must use a named volume for data persistence"
        declared = compose.get("volumes", {})
        for mount in named_mounts:
            vol_name = mount.split(":")[0]
            assert vol_name in declared, (
                f"named volume {vol_name!r} must be declared in top-level volumes:"
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
        ports = services[self.svc].get("ports") or []
        joined = " ".join(ports)
        assert "8000:8000" in joined, f"app service must publish port 8000:8000, got {ports!r}"

    def test_env_points_at_compose_services(self, services: dict):
        env = services[self.svc].get("environment", {})
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
        env = services[REQUIRED_SERVICE_NICKNAMES["app"]].get("environment", {})
        nexus_keys = [k for k in env if k.startswith("NEXUS_")]
        assert nexus_keys, (
            "app environment must set at least one NEXUS_-prefixed var "
            "(matching engine/config.py env_prefix)"
        )
        assert "NEXUS_DATABASE_URL" in nexus_keys
        assert "NEXUS_VALKEY_URL" in nexus_keys
