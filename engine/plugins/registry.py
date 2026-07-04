from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any

import structlog
import yaml

from engine.plugins.manifest import StrategyManifest

logger = structlog.get_logger()

STRATEGIES_DIR = Path(__file__).resolve().parent.parent.parent / "strategies"


def is_scoring_strategy(instance: Any) -> bool:
    try:
        from nexus_sdk.scoring import IScoringStrategy

        return isinstance(instance, IScoringStrategy)
    except ImportError:
        return False


def discover_strategies(base_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    root = base_dir or STRATEGIES_DIR
    strategies: dict[str, dict[str, Any]] = {}

    if not root.is_dir():
        logger.warning("strategies_dir_missing", path=str(root))
        return strategies

    for manifest_path in root.glob("*/manifest.yaml"):
        strategy_dir = manifest_path.parent
        name = strategy_dir.name

        with manifest_path.open() as f:
            manifest = yaml.safe_load(f)

        strategy_module_path = strategy_dir / "strategy.py"
        if not strategy_module_path.exists():
            logger.warning("strategy_module_missing", strategy=name)
            continue

        strategies[name] = {
            "manifest": manifest,
            "module_path": str(strategy_module_path),
        }
        logger.info("strategy_discovered", strategy=name)

    return strategies


def load_strategy_class(module_path: str) -> Any:
    spec = importlib.util.spec_from_file_location("strategy", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load strategy from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy_cls = getattr(module, "Strategy", None)
    if strategy_cls is None:
        logger.warning("strategy_class_not_found_in_module", path=module_path)
        raise AttributeError(f"Module {module_path} does not define a 'Strategy' class")
    return strategy_cls


def _constructor_accepts_config(cls: Any) -> bool:
    """Return ``True`` when ``cls.__init__`` can take a config argument."""
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return False
    return any(
        p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.VAR_KEYWORD,
        )
        for p in params.values()
    )


def build_strategy_instance(cls: Any, config: Any) -> Any:
    """Construct ``cls``, forwarding ``config`` when supported.

    Bundled strategies take no constructor arguments, but the
    ``instantiate`` API accepts a ``config`` (typically a
    :class:`~plugins.sdk.StrategyConfig`) so custom strategies that
    declare ``__init__(self, config)`` can receive it. Inspecting the
    signature keeps the no-arg bundled strategies working unchanged
    while making ``config`` meaningful for strategies that opt in.
    """
    if config is not None and _constructor_accepts_config(cls):
        try:
            return cls(config)
        except TypeError:
            logger.debug("strategy_config_forward_failed", cls=cls.__name__)
    return cls()


class StrategyEntry:
    """Runtime handle for a single installed strategy plugin.

    Wraps the discovered manifest + module path so the API layer can read
    metadata (:attr:`manifest`) and lifecycle state (:attr:`is_loaded`)
    without reaching into the registry's private dicts. ``instantiate()``
    loads the strategy class and records the live instance back on the
    owning :class:`PluginRegistry` so ``is_loaded`` stays accurate across
    activate / unload / reload cycles.
    """

    def __init__(
        self,
        name: str,
        manifest: dict[str, Any],
        module_path: str,
        registry: PluginRegistry,
    ) -> None:
        self.id = name
        self.name = name
        self.module_path = module_path
        self._registry = registry
        self._manifest = self._build_manifest(manifest)

    def _build_manifest(self, raw: dict[str, Any] | None) -> StrategyManifest:
        """Parse ``raw`` into a :class:`StrategyManifest`.

        Only fields declared on ``StrategyManifest`` are forwarded so
        strategy-specific extras (``parameters``, ``symbols``, …) do not
        trip strict validation. ``id`` / ``name`` default to the strategy
        directory name, which is the canonical identifier produced by
        :func:`discover_strategies`. A corrupt manifest falls back to a
        minimal valid record so the API can still describe the plugin.
        """
        data = dict(raw) if raw else {}
        data.setdefault("id", self.id)
        data.setdefault("name", self.id)
        known = set(StrategyManifest.model_fields)
        filtered = {k: v for k, v in data.items() if k in known}
        try:
            return StrategyManifest(**filtered)
        except Exception:
            logger.exception("strategy_manifest_parse_failed", strategy=self.id)
            return StrategyManifest(id=self.id, name=self.id)

    @property
    def manifest(self) -> StrategyManifest:
        return self._manifest

    @property
    def is_loaded(self) -> bool:
        return self._registry.is_strategy_loaded(self.id)

    async def instantiate(self, config: Any = None) -> Any:
        """Load the strategy class and create a live instance.

        ``config`` is forwarded to strategies whose constructor declares
        an ``__init__(self, config)``-style parameter (e.g. to receive a
        :class:`~plugins.sdk.StrategyConfig`). Bundled strategies take no
        constructor arguments and are instantiated unchanged, so the
        default ``None`` keeps every existing ``Strategy`` working while
        making ``config`` meaningful for strategies that opt in.
        """
        cls = load_strategy_class(self.module_path)
        instance = build_strategy_instance(cls, config)
        self._registry.register_instance(self.id, instance)
        return instance


class PluginRegistry:
    """Discovers and instantiates strategy plugins."""

    def __init__(self, strategies_dir: Path | None = None) -> None:
        self._strategies_dir = strategies_dir
        self._strategies = discover_strategies(strategies_dir)
        self._instances: dict[str, Any] = {}

    def is_strategy_loaded(self, strategy_id: str) -> bool:
        """Return whether ``strategy_id`` has a live instantiated instance."""
        return strategy_id in self._instances

    def register_instance(self, strategy_id: str, instance: Any) -> None:
        """Record a live ``instance`` for ``strategy_id``."""
        self._instances[strategy_id] = instance

    def load_strategy(self, strategy_name: str) -> Any | None:
        entry = self._strategies.get(strategy_name)
        if entry is None:
            logger.warning("strategy_not_found", strategy=strategy_name)
            return None
        try:
            cls = load_strategy_class(entry["module_path"])
        except (ImportError, AttributeError) as exc:
            logger.exception("strategy_load_failed", strategy=strategy_name, error=str(exc))
            return None
        try:
            return cls()
        except Exception as exc:
            logger.exception(
                "strategy_instantiation_failed",
                strategy=strategy_name,
                cls=cls.__name__,
                error=str(exc),
            )
            return None

    def list_strategies(self) -> list[str]:
        return list(self._strategies.keys())

    def get_manifest(self, strategy_name: str) -> dict[str, Any] | None:
        """Return the parsed ``manifest.yaml`` dict for ``strategy_name``.

        Returns ``None`` when the strategy is not installed. This lets
        callers read a strategy's metadata (version, description, author,
        symbols, parameters, …) without re-running :func:`discover_strategies`
        over the directory or importing the strategy module, so the registry
        stays the single source of truth for installed-strategy metadata.
        """
        entry = self._strategies.get(strategy_name)
        if entry is None:
            logger.warning("strategy_not_found", strategy=strategy_name)
            return None
        return entry.get("manifest")

    def list_all(self) -> list[dict[str, Any]]:
        """Return a JSON-serialisable summary of every installed strategy.

        Each entry carries the strategy id, display name, version and
        current load state — everything the ``GET /api/v1/strategies``
        listing needs without importing each strategy module.
        """
        summaries: list[dict[str, Any]] = []
        for name, entry in self._strategies.items():
            manifest = entry.get("manifest") or {}
            summaries.append(
                {
                    "id": name,
                    "name": manifest.get("name", name),
                    "version": manifest.get("version"),
                    "is_loaded": name in self._instances,
                }
            )
        return summaries

    def get(self, strategy_id: str) -> StrategyEntry | None:
        """Return a :class:`StrategyEntry` for ``strategy_id`` or ``None``.

        This is the rich handle the management API reads manifest metadata
        from and drives activate / deactivate / reload lifecycle through.
        """
        entry = self._strategies.get(strategy_id)
        if entry is None:
            logger.warning("strategy_not_found", strategy=strategy_id)
            return None
        return StrategyEntry(
            name=strategy_id,
            manifest=entry.get("manifest", {}),
            module_path=entry["module_path"],
            registry=self,
        )

    async def unload(self, strategy_id: str) -> None:
        """Drop any live instance for ``strategy_id`` (deactivate)."""
        if strategy_id in self._instances:
            del self._instances[strategy_id]
            logger.info("strategy_unloaded", strategy=strategy_id)

    async def reload(self, strategy_id: str) -> bool:
        """Hot-reload a strategy's manifest + code from disk.

        Re-runs discovery so manifest edits and replaced ``strategy.py``
        files are picked up, then clears any cached instance so the next
        load instantiates the fresh class. Returns ``True`` when the
        strategy is present on disk, ``False`` otherwise.
        """
        fresh = discover_strategies(self._strategies_dir)
        if strategy_id not in fresh:
            logger.warning("strategy_reload_not_found", strategy=strategy_id)
            return False
        self._strategies[strategy_id] = fresh[strategy_id]
        self._instances.pop(strategy_id, None)
        logger.info("strategy_reloaded", strategy=strategy_id)
        return True
