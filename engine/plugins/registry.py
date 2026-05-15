from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import structlog
import yaml

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


class PluginRegistry:
    """Discovers and instantiates strategy plugins."""

    def __init__(self, strategies_dir: Path | None = None, use_sandbox: bool = False) -> None:
        self._strategies = discover_strategies(strategies_dir)
        self._use_sandbox = use_sandbox

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

        if self._use_sandbox:
            return self._load_sandboxed(strategy_name, cls, entry)

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

    def _load_sandboxed(self, strategy_name: str, cls: Any, entry: dict[str, Any]) -> Any | None:
        from engine.plugins.manifest import StrategyManifest
        from engine.plugins.sandbox import PluginSandboxExecutor, SandboxPolicy

        manifest_data = entry.get("manifest", {})
        manifest_data.setdefault("id", strategy_name)
        manifest_data.setdefault("name", strategy_name)
        manifest_data.setdefault("version", "0.0.0")
        try:
            manifest = StrategyManifest(**manifest_data)
        except Exception as exc:
            logger.exception("manifest_parse_failed", strategy=strategy_name, error=str(exc))
            return None

        policy = SandboxPolicy.from_manifest(manifest)

        try:
            executor = PluginSandboxExecutor.from_factory(cls, policy)
            return executor
        except Exception as exc:
            logger.exception(
                "sandboxed_load_failed",
                strategy=strategy_name,
                error=str(exc),
            )
            return None

    def list_strategies(self) -> list[str]:
        return list(self._strategies.keys())
