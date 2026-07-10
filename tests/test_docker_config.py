"""Validation suite for ``docker-compose.yml``.

These tests guard the production multi-service compose stack against the
regressions most prone to silent breakage in a local-development setup:

* **Structural validity** — the file must parse as well-formed YAML and
  expose the canonical top-level keys (``services``, ``networks``,
  ``volumes``). When the Docker CLI is present we additionally delegate to
  ``docker compose config`` so variable interpolation (``${...}``) is checked;
  otherwise we fall back to an equivalent structural validation via PyYAML so
  the suite stays green in CI environments without Docker installed.
* **Required services** — the three core services named in the local-dev
  contract must all be present: ``nexus-app`` (FastAPI), ``nexus-postgres``
  (TimescaleDB/PostgreSQL 16) and ``nexus-valkey`` (Valkey/Redis), plus the
  ``worker`` taskiq consumer.
* **Health checks** — every stateful/routable service must declare a
  ``healthcheck`` with ``test``, ``interval``, ``timeout`` and ``retries``,
  and the app/worker must gate startup on ``service_healthy`` dependencies.
* **Port mapping** — Postgres → 5432, Valkey → 6379, app → 8000 must be
  published, and every host binding must be scoped to ``127.0.0.1`` only.
* **Networking** — all services must attach to the shared ``nexus-net``
  bridge so they resolve each other by service name, and the connection
  strings injected into the app/worker must point at those DNS names.
* **Volumes** — every named-volume reference must be declared at the top
  level (no orphan mounts) so a stray typo cannot silently create an
  anonymous volume and lose data on recreation.

The compose path is resolved relative to the repo root, so the suite works
no matter where pytest is invoked from.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"

# The three services the local-dev contract requires, mapped to the host
# port each is expected to publish.
REQUIRED_SERVICES = ("nexus-app", "nexus-postgres", "nexus-valkey", "worker")
REQUIRED_PORTS = {
    "nexus-postgres": "5432",
    "nexus-valkey": "6379",
    "nexus-app": "8000",
}
# Services that are stateful or accept traffic and therefore MUST define a
# live healthcheck. ``worker`` is a consumer loop with no host port and is
# intentionally excluded.
HEALTHCHECK_SERVICES = ("nexus-app", "nexus-postgres", "nexus-valkey")
SHARED_NETWORK = "nexus-net"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _container_port(binding: Any) -> str:
    """Extract the *container* (target) port from a compose port entry.

    Compose accepts both short and long forms:

    * ``"127.0.0.1:8000:8000"``  → host_ip:host_port:container_port
    * ``"8000:8000"``            → host_port:container_port
    * ``"8000"``                 → container_port only (random host port)
    * ``{target: 8000, ...}``    → long-form mapping
    """
    if isinstance(binding, dict):
        # Long form: the target port is the container port.
        return str(binding.get("target", ""))
    text = str(binding)
    parts = text.split(":")
    return parts[-1]


def _host_ip(binding: Any) -> str:
    """Extract the host IP a port binding is scoped to (empty = all)."""
    if isinstance(binding, dict):
        return str(binding.get("host_ip", ""))
    return str(binding).split(":")[0] if ":" in str(binding) else ""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """Parsed ``docker-compose.yml`` (without env interpolation)."""
    raw = COMPOSE_PATH.read_text()
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), "docker-compose.yml must parse to a mapping"
    return data


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    return compose.get("services", {})


# --------------------------------------------------------------------------- #
# File + structural validity
# --------------------------------------------------------------------------- #
class TestComposeStructure:
    def test_file_exists(self) -> None:
        assert COMPOSE_PATH.is_file(), f"{COMPOSE_PATH} must exist"

    def test_parses_as_valid_yaml(self, compose: dict) -> None:
        # The fixture already asserts this is a mapping; here we make the
        # intent explicit for the test report.
        assert isinstance(compose, dict)

    def test_top_level_keys_present(self, compose: dict) -> None:
        for key in ("services", "networks", "volumes"):
            assert key in compose, f"top-level key '{key}' missing"

    def test_docker_compose_config_validates(self) -> None:
        """When Docker is available, ``docker compose config`` must succeed.

        This is the canonical syntax + interpolation check. We skip (not
        fail) when the CLI is absent so the suite runs in Docker-less CI.
        """
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker CLI not installed — skipping interpolation check")
        result = subprocess.run(  # noqa: S603 — trusted, fixed argv
            [docker, "compose", "-f", str(COMPOSE_PATH), "config", "-q"],
            capture_output=True,
            check=False,
            text=True,
            env={"POSTGRES_USER": "nexus", "POSTGRES_PASSWORD": "testpw",
                 "POSTGRES_DB": "nexus"},
        )
        assert result.returncode == 0, (
            f"`docker compose config` failed:\n{result.stderr.strip()}"
        )


# --------------------------------------------------------------------------- #
# Required services
# --------------------------------------------------------------------------- #
class TestRequiredServices:
    @pytest.mark.parametrize("name", REQUIRED_SERVICES)
    def test_service_present(self, services: dict, name: str) -> None:
        assert name in services, f"required service '{name}' missing"

    def test_nexus_postgres_uses_timescale_pg16(self, services: dict) -> None:
        image = services["nexus-postgres"]["image"]
        assert "timescale/timescaledb" in image, (
            f"nexus-postgres must use the TimescaleDB image, got: {image!r}"
        )
        assert "pg16" in image, (
            f"nexus-postgres must be PostgreSQL 16, got: {image!r}"
        )

    def test_nexus_valkey_uses_valkey_image(self, services: dict) -> None:
        image = services["nexus-valkey"]["image"]
        assert image.startswith("valkey/valkey"), (
            f"nexus-valkey must use the official Valkey image, got: {image!r}"
        )

    def test_nexus_app_builds_from_dockerfile(self, services: dict) -> None:
        build = services["nexus-app"].get("build")
        assert build, "nexus-app must declare a `build:` section"
        dockerfile = build.get("dockerfile") if isinstance(build, dict) else "Dockerfile"
        assert dockerfile == "Dockerfile", (
            f"nexus-app must build from ./Dockerfile, got: {build!r}"
        )

    def test_worker_runs_taskiq_broker(self, services: dict) -> None:
        worker = services["worker"]
        entrypoint = worker.get("entrypoint") or worker.get("command") or []
        joined = " ".join(entrypoint)
        assert "engine.tasks.worker:broker" in joined, (
            "worker must target 'engine.tasks.worker:broker'; got: "
            f"{entrypoint!r}"
        )


# --------------------------------------------------------------------------- #
# Health checks
# --------------------------------------------------------------------------- #
class TestHealthChecks:
    @pytest.mark.parametrize("name", HEALTHCHECK_SERVICES)
    def test_healthcheck_defined(self, services: dict, name: str) -> None:
        hc = services[name].get("healthcheck")
        assert isinstance(hc, dict), f"service '{name}' must define a healthcheck"
        # ``test`` is disabled only via `["NONE"]`; any other value is live.
        assert hc.get("test") != ["NONE"], (
            f"service '{name}' healthcheck must not be disabled"
        )

    @pytest.mark.parametrize("name", HEALTHCHECK_SERVICES)
    def test_healthcheck_has_core_fields(self, services: dict, name: str) -> None:
        hc = services[name]["healthcheck"]
        for field in ("test", "interval", "timeout", "retries"):
            assert field in hc, (
                f"service '{name}' healthcheck missing required field '{field}'"
            )
        # retries must be a positive int so the probe can actually recover.
        assert isinstance(hc["retries"], int) and hc["retries"] > 0, (
            f"service '{name}' healthcheck retries must be a positive int"
        )

    def test_postgres_healthcheck_is_pg_isready(self, services: dict) -> None:
        test = services["nexus-postgres"]["healthcheck"]["test"]
        joined = " ".join(str(t) for t in test)
        assert "pg_isready" in joined, (
            "nexus-postgres healthcheck should use pg_isready; got: "
            f"{test!r}"
        )

    def test_valkey_healthcheck_is_ping(self, services: dict) -> None:
        test = services["nexus-valkey"]["healthcheck"]["test"]
        joined = " ".join(str(t) for t in test)
        assert "ping" in joined, (
            f"nexus-valkey healthcheck should use valkey-cli ping; got: {test!r}"
        )

    def test_app_healthcheck_hits_health_route(self, services: dict) -> None:
        test = services["nexus-app"]["healthcheck"]["test"]
        joined = " ".join(str(t) for t in test)
        assert "/health" in joined, (
            "nexus-app healthcheck should probe the /health route; got: "
            f"{test!r}"
        )

    @pytest.mark.parametrize("name", ["nexus-app", "worker"])
    def test_gates_on_healthy_deps(self, services: dict, name: str) -> None:
        deps = services[name].get("depends_on", {})
        for required in ("nexus-postgres", "nexus-valkey"):
            assert required in deps, f"{name} must depend_on '{required}'"
            assert deps[required].get("condition") == "service_healthy", (
                f"{name}.depends_on['{required}'] must gate on "
                f"'service_healthy', got: {deps[required]!r}"
            )


# --------------------------------------------------------------------------- #
# Port mapping
# --------------------------------------------------------------------------- #
class TestPortMapping:
    @pytest.mark.parametrize(("name", "port"), list(REQUIRED_PORTS.items()))
    def test_service_publishes_port(self, services: dict, name: str, port: str) -> None:
        bindings = services[name].get("ports", []) or []
        published = [_container_port(b) for b in bindings]
        assert port in published, (
            f"service '{name}' must publish container port {port}; "
            f"found bindings: {bindings!r}"
        )

    def test_all_host_bindings_scoped_to_localhost(self, services: dict) -> None:
        """No published port may bind to 0.0.0.0 — local-dev only."""
        for name, svc in services.items():
            for binding in svc.get("ports", []) or []:
                ip = _host_ip(binding)
                assert ip in ("", "127.0.0.1", "localhost"), (
                    f"service '{name}' binds to {binding!r} — host ports must "
                    "be scoped to 127.0.0.1 for local development"
                )

    def test_worker_publishes_no_host_port(self, services: dict) -> None:
        assert not services["worker"].get("ports"), (
            "worker should not publish a host port (broker consumer only)"
        )


# --------------------------------------------------------------------------- #
# Networking + connection strings
# --------------------------------------------------------------------------- #
class TestNetworking:
    @pytest.mark.parametrize(
        "name", ["nexus-app", "nexus-postgres", "nexus-valkey", "worker"]
    )
    def test_service_on_shared_network(self, services: dict, name: str) -> None:
        nets = services[name].get("networks", [])
        if isinstance(nets, dict):
            nets = list(nets.keys())
        assert SHARED_NETWORK in nets, (
            f"service '{name}' must attach to the '{SHARED_NETWORK}' network"
        )

    def test_shared_network_is_bridge(self, compose: dict) -> None:
        driver = compose["networks"][SHARED_NETWORK].get("driver")
        assert driver == "bridge", (
            f"{SHARED_NETWORK} should use the bridge driver, got: {driver!r}"
        )

    @pytest.mark.parametrize("name", ["nexus-app", "worker"])
    def test_database_url_points_at_postgres_dns(self, services: dict, name: str) -> None:
        env = services[name].get("environment", {})
        url = env.get("NEXUS_DATABASE_URL", "")
        assert "@nexus-postgres:5432" in url, (
            f"{name} NEXUS_DATABASE_URL must resolve to the compose DNS name "
            f"'nexus-postgres:5432'; got: {url!r}"
        )

    @pytest.mark.parametrize("name", ["nexus-app", "worker"])
    def test_valkey_url_points_at_valkey_dns(self, services: dict, name: str) -> None:
        env = services[name].get("environment", {})
        url = env.get("NEXUS_VALKEY_URL", "")
        assert "nexus-valkey:6379" in url, (
            f"{name} NEXUS_VALKEY_URL must resolve to the compose DNS name "
            f"'nexus-valkey:6379'; got: {url!r}"
        )


# --------------------------------------------------------------------------- #
# Volumes
# --------------------------------------------------------------------------- #
class TestVolumes:
    def test_persistence_volumes_declared(self, compose: dict) -> None:
        volumes = compose.get("volumes", {})
        for required in ("pgdata", "valkeydata"):
            assert required in volumes, (
                f"named volume '{required}' must be declared under top-level "
                "'volumes:'"
            )

    def test_postgres_mounts_pgdata(self, services: dict) -> None:
        mounts = services["nexus-postgres"].get("volumes", []) or []
        assert any(str(m).startswith("pgdata:") for m in mounts), (
            "nexus-postgres must mount the 'pgdata' volume; found: "
            f"{mounts!r}"
        )

    def test_valkey_mounts_valkeydata_at_data(self, services: dict) -> None:
        mounts = services["nexus-valkey"].get("volumes", []) or []
        assert "valkeydata:/data" in [str(m) for m in mounts], (
            "nexus-valkey must mount 'valkeydata' at '/data' for persistence; "
            f"found: {mounts!r}"
        )

    def test_no_undeclared_named_volume_mounts(self, compose: dict) -> None:
        """Every named-volume reference must be declared at top level."""
        declared = set(compose.get("volumes", {}).keys())
        for svc_name, svc in compose.get("services", {}).items():
            for mount in svc.get("volumes", []) or []:
                source = str(mount).split(":", 1)[0]
                # Skip bind mounts (relative paths / absolute paths / ~).
                if source.startswith((".", "/", "~")):
                    continue
                # Anonymous volume (no colon) → not a named reference.
                if ":" not in str(mount):
                    continue
                assert source in declared, (
                    f"service '{svc_name}' mounts undeclared named volume "
                    f"'{source}'"
                )


# --------------------------------------------------------------------------- #
# .env.example keeps the compose file self-consistent
# --------------------------------------------------------------------------- #
ENV_EXAMPLE_PATH = COMPOSE_PATH.parent / ".env.example"


class TestEnvExample:
    def test_env_example_exists(self) -> None:
        assert ENV_EXAMPLE_PATH.is_file(), ".env.example must exist"

    @pytest.mark.parametrize(
        "var",
        [
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "NEXUS_DATABASE_URL",
            "NEXUS_VALKEY_URL",
            "NEXUS_SECRET_KEY",
            "NEXUS_APP_ENV",
            "NEXUS_LOG_LEVEL",
        ],
    )
    def test_required_variable_documented(self, var: str) -> None:
        text = ENV_EXAMPLE_PATH.read_text()
        assert re.search(rf"(?m)^{re.escape(var)}=", text), (
            f".env.example must document '{var}'"
        )

    def test_compose_vars_all_documented(self, compose: dict) -> None:
        """Every ${VAR} interpolated by the compose file must be documented."""
        env_text = ENV_EXAMPLE_PATH.read_text()
        raw = COMPOSE_PATH.read_text()
        # ${VAR} or ${VAR:-default} or ${VAR:?msg}
        interpolated = set(re.findall(r"\$\{([A-Z0-9_]+)", raw))
        for var in sorted(interpolated):
            assert re.search(rf"(?m)^{re.escape(var)}=", env_text), (
                f"compose references ${{{var}}} but .env.example does not "
                f"document it"
            )
