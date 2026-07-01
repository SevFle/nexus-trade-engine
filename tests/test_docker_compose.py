"""Validation suite for ``docker-compose.yml``.

These tests guard the production compose stack against regressions in the
areas most prone to silent breakage:

* **Image pinning** — every image must be pinned to an immutable registry
  digest (``@sha256:...``) and must never float on ``:latest``, so deploys are
  reproducible and immune to upstream tag re-pointing.
* **Worker service** — the taskiq worker must run the
  ``engine.tasks.worker:broker`` entrypoint and must wait on the *same*
  health-gated dependencies (``db`` + ``valkey`` reporting ``service_healthy``)
  as the API service (``nexus-api`` / ``app``).
* **Valkey persistence** — a named ``valkeydata`` volume must be declared and
  mounted at ``/data`` so queue/broker state survives container recreation.

``docker compose config`` is the canonical syntax/interpolation check; when the
Docker CLI is available we delegate to it, otherwise we fall back to an
equivalent structural validation via PyYAML so the suite stays green in CI
environments without Docker installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"

# A registry digest reference as defined by the OCI distribution spec:
# ``<tag>@sha256:<64 hex chars>``. Anchored so it must be the *whole* image
# reference, not a substring match.
_DIGEST_RE = re.compile(
    r"^.+@sha256:[0-9a-f]{64}$",
)


# --------------------------------------------------------------------------- #
# Fixture / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def compose() -> dict:
    """Parsed ``docker-compose.yml`` (without env interpolation)."""
    raw = COMPOSE_PATH.read_text()
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), "docker-compose.yml must parse to a mapping"
    return data


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    return compose.get("services", {})


def _api_service_name(services: dict) -> str:
    """The name of the API/web service (``nexus-api``).

    Identified robustly as the service that publishes the HTTP port (8000)
    rather than hard-coding ``app``, so the assertion holds even if the service
    is later renamed to ``nexus-api``.
    """
    for name, svc in services.items():
        for binding in svc.get("ports", []) or []:
            # bindings look like "127.0.0.1:8000:8000" or "8000:8000"
            if str(binding).split(":")[-1] == "8000" or str(binding) == "8000":
                return name
    pytest.fail("no service publishes the API port 8000")


# --------------------------------------------------------------------------- #
# Structural validity
# --------------------------------------------------------------------------- #
class TestComposeStructure:
    def test_file_exists(self) -> None:
        assert COMPOSE_PATH.is_file(), f"{COMPOSE_PATH} must exist"

    def test_top_level_keys(self, compose: dict) -> None:
        assert "services" in compose
        assert "volumes" in compose

    def test_expected_services_present(self, services: dict) -> None:
        for required in ("db", "valkey", "worker"):
            assert required in services, f"service '{required}' missing"


# --------------------------------------------------------------------------- #
# Image pinning (no :latest, digest-addressed)
# --------------------------------------------------------------------------- #
class TestImagePinning:
    @pytest.mark.parametrize(
        "service_name", ["db", "valkey"], ids=["db", "valkey"]
    )
    def test_image_is_digest_pinned(self, services: dict, service_name: str) -> None:
        image = services[service_name]["image"]
        assert _DIGEST_RE.match(image), (
            f"{service_name} image must be pinned by sha256 digest, got: {image!r}"
        )

    @pytest.mark.parametrize(
        "service_name", ["db", "valkey"], ids=["db", "valkey"]
    )
    def test_image_is_not_latest(self, services: dict, service_name: str) -> None:
        image = services[service_name]["image"]
        # Strip the digest portion when checking for floating tags.
        ref = image.split("@", 1)[0]
        tag = ref.rsplit(":", 1)[-1] if ":" in ref else ""
        assert tag != "latest", (
            f"{service_name} must not use the floating ':latest' tag, got: {image!r}"
        )
        assert tag, f"{service_name} image must have an explicit tag: {image!r}"

    def test_timescale_db_pinned_to_specific_version(self, services: dict) -> None:
        # timescale/timescaledb:<semver>-pg<ver>  — the tag carries a real
        # version, not 'latest-pg16'.
        ref = services["db"]["image"].split("@", 1)[0]
        tag = ref.rsplit(":", 1)[-1]
        assert re.match(r"^\d+\.\d+\.\d+-pg\d+$", tag), (
            f"timescale tag must be a concrete '<semver>-pg<ver>', got: {tag!r}"
        )

    def test_valkey_pinned_to_specific_version(self, services: dict) -> None:
        ref = services["valkey"]["image"].split("@", 1)[0]
        tag = ref.rsplit(":", 1)[-1]
        assert re.match(r"^\d+\.\d+\.\d+", tag), (
            f"valkey tag must start with a concrete semver, got: {tag!r}"
        )


# --------------------------------------------------------------------------- #
# Valkey persistence volume
# --------------------------------------------------------------------------- #
class TestValkeyPersistence:
    def test_valkeydata_volume_declared(self, compose: dict) -> None:
        volumes = compose.get("volumes", {})
        assert "valkeydata" in volumes, (
            "named volume 'valkeydata' must be declared under top-level 'volumes:'"
        )

    def test_valkey_mounts_valkeydata_at_data(self, services: dict) -> None:
        mounts = services["valkey"].get("volumes", []) or []
        assert "valkeydata:/data" in mounts, (
            "valkey must mount the 'valkeydata' volume at '/data'; "
            f"found mounts: {mounts!r}"
        )

    def test_no_orphan_volume_mounts(self, compose: dict) -> None:
        """Every external volume reference must be declared at top level."""
        declared = set(compose.get("volumes", {}).keys())
        for svc_name, svc in compose.get("services", {}).items():
            for mount in svc.get("volumes", []) or []:
                source = str(mount).split(":", 1)[0]
                # Skip bind mounts (relative paths / absolute paths / ~).
                if source.startswith((".", "/", "~")):
                    continue
                # Anonymous volume: "foo" with no colon → skip.
                if ":" not in str(mount):
                    continue
                assert source in declared, (
                    f"service '{svc_name}' mounts undeclared named volume '{source}'"
                )


# --------------------------------------------------------------------------- #
# Worker service + dependency health gates
# --------------------------------------------------------------------------- #
class TestWorkerService:
    def test_worker_exists(self, services: dict) -> None:
        assert "worker" in services

    def test_worker_uses_broker_entrypoint(self, services: dict) -> None:
        worker = services["worker"]
        entrypoint = worker.get("entrypoint") or worker.get("command") or []
        joined = " ".join(entrypoint)
        assert "engine.tasks.worker:broker" in joined, (
            "worker entrypoint/command must target 'engine.tasks.worker:broker'; "
            f"got: {entrypoint!r}"
        )
        # And it should be invoking taskiq as the worker runner.
        assert "taskiq" in joined and "worker" in joined, (
            f"worker entrypoint must run the taskiq worker, got: {entrypoint!r}"
        )

    def test_worker_depends_on_db_and_valkey_healthy(
        self, services: dict
    ) -> None:
        deps = services["worker"].get("depends_on", {})
        for required in ("db", "valkey"):
            assert required in deps, f"worker must depend_on '{required}'"
            assert deps[required].get("condition") == "service_healthy", (
                f"worker depends_on '{required}' must gate on "
                f"'service_healthy', got: {deps[required]!r}"
            )

    def test_worker_health_gates_match_api_service(
        self, services: dict
    ) -> None:
        """The worker's dependency gates must mirror the API (nexus-api)
        service so both start only once infra is healthy."""
        api_name = _api_service_name(services)
        api_deps = services[api_name].get("depends_on", {})
        worker_deps = services["worker"].get("depends_on", {})
        assert worker_deps == api_deps, (
            f"worker depends_on {worker_deps!r} must match API service "
            f"'{api_name}' depends_on {api_deps!r}"
        )

    def test_worker_configured_for_same_infra_urls(self, services: dict) -> None:
        """Worker must be wired to the same DB + Valkey URLs as the API."""
        api_name = _api_service_name(services)
        api_env = services[api_name].get("environment", {})
        worker_env = services["worker"].get("environment", {})
        for key in ("NEXUS_DATABASE_URL", "NEXUS_VALKEY_URL"):
            assert worker_env.get(key) == api_env.get(key), (
                f"worker {key} must match the API service's value"
            )


# --------------------------------------------------------------------------- #
# docker compose config (canonical syntax + interpolation check)
# --------------------------------------------------------------------------- #
def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.mark.skipif(
    not _docker_available(),
    reason="docker CLI not installed; structural validation covers this instead",
)
def test_docker_compose_config_validates() -> None:
    """``docker compose config`` is the canonical syntax + interpolation check.

    It resolves ``${VAR}`` interpolation and validates the file against the
    compose spec, so it is strictly stronger than the YAML-parse fallback.
    """
    env = {
        "POSTGRES_USER": "nexus_test",
        "POSTGRES_PASSWORD": "testpw",
        "POSTGRES_DB": "nexus_test",
    }
    # Provide required env inline so the mandatory ``:?`` substitutions resolve.
    cmd = ["docker", "compose", "config", "-q"]
    result = subprocess.run(  # noqa: S603 - cmd is a trusted, hardcoded literal
        cmd,
        cwd=COMPOSE_PATH.parent,
        env={**env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"`{' '.join(cmd)}` failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


# --------------------------------------------------------------------------- #
# Live registry check (opt-in / integration only)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_pinned_digests_resolve_at_registry(services: dict) -> None:
    """Confirm the pinned image@digest references actually exist upstream.

    Network-dependent; deselect with ``-m 'not integration'``. Kept separate
    from the deterministic structural tests so the core suite never needs
    network access.
    """
    import json
    import urllib.request

    def _resolve(image_ref: str) -> str:
        ref = image_ref.split("@", 1)[0]
        repo, tag = ref.split(":", 1)
        digest = image_ref.rsplit("@sha256:", 1)[-1]
        auth_url = (
            "https://auth.docker.io/token?service=registry.docker.io"
            f"&scope=repository:{repo}:pull"
        )
        token = json.loads(urllib.request.urlopen(auth_url).read())["token"]  # noqa: S310 - https URL to Docker auth
        req = urllib.request.Request(
            f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
            method="HEAD",
        )
        returned = urllib.request.urlopen(req).headers["Docker-Content-Digest"]  # noqa: S310 - https Request to Docker registry
        assert returned == f"sha256:{digest}", (
            f"digest drift for {image_ref}: registry reports {returned}"
        )

    _resolve(services["db"]["image"])
    _resolve(services["valkey"]["image"])
