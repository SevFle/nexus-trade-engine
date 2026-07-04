"""Drift guard: docs must not contradict the fixed strategies REST surface.

Background
----------
The ``/api/v1/strategies/*`` management routes used to raise
``AttributeError`` at runtime because:

1. ``create_app()``'s lifespan never attached ``app.state.plugin_registry``,
   and
2. :class:`~engine.plugins.registry.PluginRegistry` lacked the
   ``list_all`` / ``get`` / ``unload`` / ``reload`` methods the routes call.

That drift was landed in the same changeset as a ``docs/known-limitations.md``
P0 entry and a ``docs/api-reference.md`` "Status: broken at runtime." callout —
so the documentation advertised a *current* P0 bug while the code had already
been fixed. That doc-vs-code contradiction is the loop these tests break.

What these tests pin down
-------------------------
* The runtime fix is in place (the management methods exist and the lifespan
  sets the registry). This is the *positive* contract; if it regresses the
  docs are then legitimately allowed to re-flag the bug.
* The documentation no longer describes the routes as broken *now*
  ("Status: broken at runtime.", "P0 — ``/api/v1/strategies/*`` routes raise
  at runtime"). Historical notes phrased in the past tense are fine.
* The built static site (``site/...``) is regenerated to match the corrected
  source docs, so the public site does not keep serving stale "broken" copy
  after the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"
SITE = REPO_ROOT / "site"


def _read(path: Path) -> str:
    assert path.is_file(), f"expected doc to exist: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def api_reference_md() -> str:
    return _read(DOCS / "api-reference.md")


@pytest.fixture(scope="module")
def known_limitations_md() -> str:
    return _read(DOCS / "known-limitations.md")


class TestStrategiesRuntimeFixIsInPlace:
    """Positive contract: the code surface the routes rely on must exist.

    These are the preconditions that make the doc claims stale. If any of
    these fail, the bug is genuinely back and the docs are *correct* to
    flag it — so this class failing should prompt re-adding the limitation,
    not loosening the doc assertions below.
    """

    def test_plugin_registry_exposes_management_surface(self):
        from engine.plugins.registry import PluginRegistry

        for method in ("list_all", "get", "unload", "reload"):
            assert callable(
                getattr(PluginRegistry, method)
            ), f"PluginRegistry.{method} must exist for the management routes"

    def test_plugin_registry_get_returns_entry_with_lifecycle_attrs(self):
        from engine.plugins.registry import StrategyEntry

        # StrategyEntry is the value object the routes read .manifest /
        # .is_loaded / .instantiate() off. Its presence is what makes
        # GET/POST /strategies work.
        entry_attrs = {"manifest", "is_loaded", "instantiate"}
        missing = entry_attrs - set(dir(StrategyEntry))
        assert not missing, f"StrategyEntry missing lifecycle attrs: {missing}"

    def test_strategies_router_uses_registry_management_methods(self):
        """The routes must keep reading app.state.plugin_registry (the fixed
        wiring path), not regress to a missing attribute."""
        routes = (REPO_ROOT / "engine/api/routes/strategies.py").read_text(
            encoding="utf-8"
        )
        assert "request.app.state.plugin_registry" in routes

    def test_create_app_lifespan_sets_plugin_registry(self):
        """The lifespan startup handler must set app.state.plugin_registry.

        We assert against the source rather than running the lifespan (covered
        exhaustively by test_strategies_app_integration.py) so this guard is
        fast and dependency-free.
        """
        app_src = (REPO_ROOT / "engine/app.py").read_text(encoding="utf-8")
        assert "app.state.plugin_registry = PluginRegistry()" in app_src, (
            "create_app() lifespan must set app.state.plugin_registry so the "
            "first request doesn't raise AttributeError"
        )


class TestApiReferenceDoesNotCallRoutesBroken:
    """``docs/api-reference.md`` must not advertise the strategies routes as
    broken in the present tense. A past-tense historical note is acceptable."""

    def test_no_present_tense_broken_callout(self, api_reference_md):
        # The exact stale callout that was added next to the Strategies table.
        assert "Status: broken at runtime." not in api_reference_md, (
            "api-reference.md still carries a present-tense 'Status: broken at "
            "runtime.' callout for the strategies routes, but the bug is fixed "
            "(see test_strategies_app_integration.py). Downgrade it to a "
            "historical note or remove it."
        )

    def test_no_present_tense_attribute_error_claim(self, api_reference_md):
        # The stale callout claimed routes "raise AttributeError through the
        # production entry point". That present-tense claim must be gone.
        assert "raise\n> `AttributeError`" not in api_reference_md
        assert "raise AttributeError" not in api_reference_md.replace(
            "\n> ", " "
        )


class TestKnownLimitationsHasNoStrategiesP0:
    """``docs/known-limitations.md`` must not list the strategies routes as a
    current P0 limitation now that the runtime fix has landed."""

    def test_no_p0_strategies_heading(self, known_limitations_md):
        assert "## P0 — `/api/v1/strategies/*`" not in known_limitations_md, (
            "known-limitations.md still lists the strategies routes as a P0 "
            "limitation, but the registry mismatch is fixed. Move it to a "
            "resolved/historical section or remove it."
        )

    def test_no_present_tense_raise_at_runtime_claim(self, known_limitations_md):
        # "routes raise at runtime (registry mismatch)" — present tense.
        assert "routes raise at runtime" not in known_limitations_md

    def test_no_strategies_rest_broken_anchor_pointing_nowhere(self, known_limitations_md):
        # The '<a id="strategies-rest-broken"></a>' anchor existed solely for
        # the removed P0 entry. api-reference.md links to it; once the entry is
        # gone the anchor must be gone too so the link isn't dangling.
        if "strategies-rest-broken" in known_limitations_md:
            # Allowed only inside a historical/resolved note, not a current P0.
            assert "## P0 —" not in known_limitations_md.split(
                "strategies-rest-broken", 1
            )[1].split("\n## ", 1)[0]


class TestStaticSiteMatchesSourceDocs:
    """The built MkDocs site must not lag behind the corrected source docs.

    If the docs are fixed but ``site/`` is not regenerated, the public site
    keeps serving the stale 'broken at runtime' copy — the same contradiction
    the runtime tests already prevent at the source level. These checks force
    a ``mkdocs build`` after any doc correction.
    """

    @pytest.fixture(scope="module")
    def api_reference_html(self) -> str:
        return _read(SITE / "api-reference/index.html")

    @pytest.fixture(scope="module")
    def known_limitations_html(self) -> str:
        return _read(SITE / "known-limitations/index.html")

    def test_site_api_reference_no_broken_callout(self, api_reference_html):
        assert "Status: broken at runtime." not in api_reference_html, (
            "site/api-reference/index.html still serves the stale 'broken at "
            "runtime' callout — run `mkdocs build` to regenerate the site."
        )

    def test_site_known_limitations_no_present_p0_strategies(self, known_limitations_html):
        # The HTML renders the heading text inline; check the readable phrase.
        assert "routes raise at runtime" not in known_limitations_html, (
            "site/known-limitations/index.html still lists the strategies "
            "routes as a current P0 ('routes raise at runtime'). Regenerate "
            "the site after removing the P0 entry from the source doc."
        )

    def test_site_known_limitations_no_p0_heading_for_strategies(self, known_limitations_html):
        # MkDocs slugifies the heading to an id like
        # p0-apiv1strategies-routes-raise-at-runtime-registry-mismatch
        assert "p0-apiv1strategies" not in known_limitations_html.lower(), (
            "site/known-limitations/index.html still renders the strategies P0 "
            "heading. Regenerate the site."
        )
