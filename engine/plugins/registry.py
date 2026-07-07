from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import structlog
import yaml

from engine.plugins.allowlist import DENYLIST_MODULES
from engine.plugins.restricted_importer import ImportValidator

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

    # Layer-0 static check: reject strategy source that imports blocked
    # modules or invokes code-execution builtins (``exec``/``eval``/
    # ``compile``/``__import__``/``importlib.import_module``) *before*
    # ``exec_module`` runs any of it.  This fails fast and side-effect free,
    # complementing the runtime :class:`RestrictedImporter` hooks.  Reading
    # the file here is safe: restrictions are not active while the host loads
    # strategies, so plain ``open`` is used.
    with open(module_path, encoding="utf-8") as f:
        source = f.read()
    violations = ImportValidator(DENYLIST_MODULES).validate(source)
    if violations:
        joined = "; ".join(violations)
        logger.warning(
            "strategy_source_blocked",
            path=module_path,
            violations=violations,
        )
        raise ImportError(
            f"Strategy source {module_path} rejected by import validator: {joined}"
        )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy_cls = getattr(module, "Strategy", None)
    if strategy_cls is None:
        logger.warning("strategy_class_not_found_in_module", path=module_path)
        raise AttributeError(f"Module {module_path} does not define a 'Strategy' class")
    return strategy_cls


class PluginRegistry:
    """Discovers and instantiates strategy plugins."""

    def __init__(self, strategies_dir: Path | None = None) -> None:
        self._strategies = discover_strategies(strategies_dir)

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

    def get_module_path(self, strategy_name: str) -> str | None:
        """Return the on-disk path of ``strategy.py`` for ``strategy_name``.

        Returns ``None`` when the strategy is not installed. Together with
        :meth:`get_manifest` this exposes everything the MCP server needs to
        describe a strategy (metadata + code location) without importing the
        plugin, keeping the registry the single source of truth.
        """
        entry = self._strategies.get(strategy_name)
        if entry is None:
            logger.warning("strategy_not_found", strategy=strategy_name)
            return None
        return entry.get("module_path")
