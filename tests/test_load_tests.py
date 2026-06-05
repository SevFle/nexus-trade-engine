"""Static validation for the k6 load-test suite.

The scripts in ``tests/load/`` are JavaScript executed by k6, so they're
outside the normal pytest surface. This module gives us a single place to
catch the most common breakage modes before they hit the weekly CI cron:

* The k6 workflow YAML parses.
* Every JS file is syntactically valid (``node -c``).
* Every endpoint the scripts hit actually exists in the FastAPI router.
* The threshold table in the operator runbook matches the real thresholds
  in ``api-baseline.js``.

Together these guarantee that a green CI run on the load-test workflow
means a green run on staging.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LOAD_DIR = ROOT / "tests" / "load"
WORKFLOW = ROOT / ".github" / "workflows" / "load-test.yml"
RUNBOOK = ROOT / "docs" / "operations" / "load-testing.md"

JS_FILES = [
    LOAD_DIR / "lib" / "auth.js",
    LOAD_DIR / "api-smoke.js",
    LOAD_DIR / "api-baseline.js",
]


@pytest.fixture(scope="module")
def app_routes() -> set[str]:
    """Collect the (path, method) surface mounted on the FastAPI app.

    Done once per module — ``create_app`` is cheap but not free, and
    every test in this file just needs the same set of paths.
    """
    from engine.app import create_app

    app = create_app()
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    return paths


@pytest.fixture(scope="module")
def app_route_methods() -> dict[str, set[str]]:
    """Map ``path -> {methods}`` (HEAD excluded) for method-aware checks."""
    from engine.app import create_app

    app = create_app()
    out: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            out.setdefault(path, set()).update(
                m for m in methods if m != "HEAD"
            )
    return out


class TestFiles:
    def test_all_js_files_exist(self) -> None:
        for path in JS_FILES:
            assert path.is_file(), f"missing k6 script: {path}"

    def test_readme_exists(self) -> None:
        assert (LOAD_DIR / "README.md").is_file()

    def test_workflow_exists(self) -> None:
        assert WORKFLOW.is_file()

    def test_runbook_exists(self) -> None:
        assert RUNBOOK.is_file()


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed — skipping JS syntax checks",
)
class TestJsSyntax:
    @pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
    def test_node_syntax_check(self, path: Path) -> None:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["node", "-c", str(path)],  # noqa: S607 — node resolved via PATH intentionally
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"node -c {path.name} failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestWorkflow:
    def test_yaml_parses(self) -> None:
        data = yaml.safe_load(WORKFLOW.read_text())
        assert "jobs" in data, "workflow has no jobs"
        assert "run" in data["jobs"], "workflow is missing the 'run' job"

    def test_triggers(self) -> None:
        data = yaml.safe_load(WORKFLOW.read_text())
        triggers = data.get(True) or data.get("on") or {}
        assert "workflow_dispatch" in triggers
        assert "schedule" in triggers

    def test_manual_inputs(self) -> None:
        data = yaml.safe_load(WORKFLOW.read_text())
        triggers = data.get(True) or data.get("on") or {}
        wd = triggers["workflow_dispatch"]
        assert "scenario" in wd["inputs"]
        assert "base_url" in wd["inputs"]
        options = wd["inputs"]["scenario"]["options"]
        assert "smoke" in options
        assert "baseline" in options

    def test_weekly_cron(self) -> None:
        data = yaml.safe_load(WORKFLOW.read_text())
        triggers = data.get(True) or data.get("on") or {}
        cron_expr = triggers["schedule"][0]["cron"]
        # Monday 03:00 UTC — k6 docs/operations/load-testing.md commits
        # to this exact window, so pin it.
        assert cron_expr == "0 3 * * 1", (
            f"weekly baseline cron changed: {cron_expr!r}"
        )

    def test_summary_artifact(self) -> None:
        text = WORKFLOW.read_text()
        assert "load-test-summary.json" in text, (
            "workflow must upload load-test-summary.json for regression diffs"
        )
        assert "retention-days: 30" in text, (
            "workflow must retain the summary artifact for 30 days"
        )


class TestScriptRoutes:
    """The k6 scripts must hit endpoints that actually exist."""

    @pytest.fixture(scope="class")
    def smoke_text(self) -> str:
        return (LOAD_DIR / "api-smoke.js").read_text()

    @pytest.fixture(scope="class")
    def baseline_text(self) -> str:
        return (LOAD_DIR / "api-baseline.js").read_text()

    def test_health_route_is_root_not_api_v1(self, smoke_text: str) -> None:
        # The health router is mounted without the /api/v1 prefix; if a
        # future refactor moves it under /api/v1/health, update both the
        # script and this assertion together.
        assert "${data.baseUrl}/health" in smoke_text
        assert "/api/v1/health" not in smoke_text

    def test_auth_login_route_exists(
        self, app_route_methods: dict[str, set[str]]
    ) -> None:
        assert "POST" in app_route_methods.get("/api/v1/auth/login", set())

    def test_portfolio_route_exists(
        self,
        app_route_methods: dict[str, set[str]],
        smoke_text: str,
        baseline_text: str,
    ) -> None:
        assert "/api/v1/portfolio" in app_route_methods or (
            "/api/v1/portfolio/" in app_route_methods
        ), "portfolio route missing — k6 scripts will 404"
        assert "/api/v1/portfolio" in smoke_text
        assert "/api/v1/portfolio" in baseline_text

    def test_reference_suggest_route_exists(
        self,
        app_route_methods: dict[str, set[str]],
        smoke_text: str,
        baseline_text: str,
    ) -> None:
        # The reference router only exposes /suggest, not /exchanges.
        assert "GET" in app_route_methods.get(
            "/api/v1/reference/suggest", set()
        ), "GET /api/v1/reference/suggest must exist for the k6 scripts"
        assert "/api/v1/reference/suggest" in smoke_text
        assert "/api/v1/reference/suggest" in baseline_text
        # Guard against drift back to the old, non-existent endpoint.
        assert "/api/v1/reference/exchanges" not in smoke_text
        assert "/api/v1/reference/exchanges" not in baseline_text

    def test_backtest_run_route_exists(
        self,
        app_route_methods: dict[str, set[str]],
        baseline_text: str,
    ) -> None:
        assert "POST" in app_route_methods.get(
            "/api/v1/backtest/run", set()
        ), "POST /api/v1/backtest/run must exist for the baseline script"
        assert "/api/v1/backtest/run" in baseline_text
        # The bare /api/v1/backtest path is a 404 — guard against regressions.
        assert "'${data.baseUrl}/api/v1/backtest'," not in baseline_text
        assert "'${data.baseUrl}/api/v1/backtest'" not in baseline_text

    def test_backtest_payload_matches_pydantic_model(
        self, baseline_text: str
    ) -> None:
        # engine.api.routes.backtest.BacktestRequest requires these exact
        # field names. A rename in the Pydantic model is the #1 way this
        # script silently breaks — pin the names here.
        assert "strategy_name" in baseline_text
        assert "start_date" in baseline_text
        assert "end_date" in baseline_text
        # The old field names would silently 422.
        assert "strategy_id" not in baseline_text
        assert not re.search(r"\bstart\s*:", baseline_text)
        assert not re.search(r"\bend\s*:", baseline_text)


class TestThresholds:
    """The runbook's threshold table must match the baseline script."""

    @pytest.fixture(scope="class")
    def baseline_text(self) -> str:
        return (LOAD_DIR / "api-baseline.js").read_text()

    @pytest.fixture(scope="class")
    def runbook_text(self) -> str:
        return RUNBOOK.read_text()

    @pytest.mark.parametrize(
        ("tag", "p95"),
        [
            ("portfolio_list", "800"),
            ("reference_suggest", "400"),
            ("backtest_submit", "1500"),
        ],
    )
    def test_p95_threshold_listed(
        self, baseline_text: str, runbook_text: str, tag: str, p95: str
    ) -> None:
        # Must be enforced by the script…
        assert (
            f"http_req_duration{{name:{tag}}}" in baseline_text
        ), f"baseline script is missing threshold for tag {tag!r}"
        assert f"p(95)<{p95}" in baseline_text
        # …and documented in the runbook.
        assert tag in runbook_text, (
            f"runbook must list threshold for tag {tag!r}"
        )
        assert f"p(95) < {p95}" in runbook_text or f"p(95)<{p95}" in runbook_text

    def test_failed_rate_threshold(self, baseline_text: str, runbook_text: str) -> None:
        assert "http_req_failed" in baseline_text
        assert "rate<0.005" in baseline_text
        assert "0.5%" in runbook_text
