"""Tests for :mod:`engine.plugins.sdk` — the ``BaseStrategy`` ABC.

``engine.plugins.sdk`` defines the contract every user strategy extends:
an abstract ``on_bar`` plus optional ``on_start``/``on_end`` hooks.  These
tests exercise the ABC directly so the module is fully covered (it was
previously only exercised transitively via concrete strategies, leaving the
default hook implementations and class-level defaults untested).
"""

from __future__ import annotations

import pytest

from engine.plugins.sdk import BaseStrategy


class _RunningStrategy(BaseStrategy):
    """Minimal concrete subclass used as the happy-path fixture."""

    name = "running"
    version = "2.3.4"

    def on_bar(self, state, portfolio):
        return [{"strategy": self.name, "side": "buy"}]


class _UnnamedStrategy(BaseStrategy):
    """Subclass that relies on the ``BaseStrategy`` name/version defaults."""

    def on_bar(self, state, portfolio):
        return []


class TestBaseStrategyAbstractness:
    def test_cannot_instantiate_abstract_base_directly(self):
        # ``on_bar`` is abstract, so the ABC refuses construction.
        with pytest.raises(TypeError):
            BaseStrategy()  # type: ignore[abstract]

    def test_concrete_subclass_without_on_bar_is_still_abstract(self):
        # A subclass that forgets to implement ``on_bar`` stays abstract.
        class Incomplete(BaseStrategy):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


class TestBaseStrategyDefaults:
    def test_class_level_name_and_version_defaults(self):
        assert BaseStrategy.name == "unnamed"
        assert BaseStrategy.version == "0.1.0"

    def test_subclass_inherits_defaults_when_not_overridden(self):
        strategy = _UnnamedStrategy()
        assert strategy.name == "unnamed"
        assert strategy.version == "0.1.0"

    def test_subclass_can_override_name_and_version(self):
        strategy = _RunningStrategy()
        assert strategy.name == "running"
        assert strategy.version == "2.3.4"


class TestBaseStrategyHooks:
    def test_on_bar_returns_the_orders_list(self):
        strategy = _RunningStrategy()
        orders = strategy.on_bar(state=object(), portfolio=object())
        assert orders == [{"strategy": "running", "side": "buy"}]

    def test_on_start_default_is_a_noop_returning_none(self):
        # The optional ``on_start`` hook must accept a portfolio and be a no-op.
        strategy = _RunningStrategy()
        assert strategy.on_start(portfolio=object()) is None

    def test_on_end_default_is_a_noop_returning_none(self):
        # The optional ``on_end`` hook must accept a portfolio and be a no-op.
        strategy = _RunningStrategy()
        assert strategy.on_end(portfolio=object()) is None

    def test_default_hooks_are_inherited_unchanged(self):
        # A subclass that does not override the hooks uses the base no-ops.
        strategy = _UnnamedStrategy()
        assert strategy.on_start(portfolio=object()) is None
        assert strategy.on_end(portfolio=object()) is None
