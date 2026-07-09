"""Happy-path test for ``GET /api/v1/tasks/status``.

Drives the full ``create_app()`` ASGI app via Starlette's ``TestClient``
to assert the taskiq health endpoint returns the documented JSON shape.
The lifespan is intentionally *not* entered (no ``with`` block) so the
test stays hermetic: it requires no live Valkey/Redis, event bus or DB
connection, mirroring how the rest of the route-level suite drives
``create_app()`` via a transport without booting the lifespan.

A companion concern — that the lifespan actually invokes
``broker.startup()`` / ``broker.shutdown()`` — is covered by
``tests/test_task_broker.py`` at the broker-construction layer; this
test pins the public HTTP contract of the status endpoint.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from engine.app import create_app


def test_tasks_status_endpoint_returns_ok() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/tasks/status")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "broker": "running"}
