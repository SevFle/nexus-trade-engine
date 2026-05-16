from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import structlog
import yaml
from opentelemetry import trace

from engine.plugins.plugin_signing import PluginSigner

logger = structlog.get_logger()
_tracer = trace.get_tracer(__name__)

STRATEGIES_DIR = Path(__file__).resolve().parent.parent.parent / "strategies"


def is_scoring_strategy(instance: Any) -> bool:
    with _tracer.start_as_current_span("registry.is_scoring_strategy") as span:
        try:
            from nexus_sdk.scoring import IScoringStrategy

            return isinstance(instance, IScoringStrategy)
        except ImportError:
            return False
        except Exception as exc:
            span.set_status(trace.StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise


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
    with _tracer.start_as_current_span("registry.load_strategy_class") as span:
        span.set_attribute("module_path", module_path)

        def _cannot_load() -> None:
            raise ImportError(f"Cannot load strategy from {module_path}")

        def _no_strategy_class() -> None:
            raise AttributeError(
                f"Module {module_path} does not define a 'Strategy' class"
            )

        try:
            spec = importlib.util.spec_from_file_location("strategy", module_path)
            if spec is None or spec.loader is None:
                _cannot_load()
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except FileNotFoundError as exc:
                raise ImportError(f"Cannot load strategy from {module_path}") from exc
            strategy_cls = getattr(module, "Strategy", None)
            if strategy_cls is None:
                logger.warning("strategy_class_not_found_in_module", path=module_path)
                _no_strategy_class()
        except Exception as exc:
            span.set_status(trace.StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise
        else:
            return strategy_cls


class PluginRegistry:
    def __init__(self, strategies_dir: Path | None = None, use_sandbox: bool = False) -> None:
        self._strategies = discover_strategies(strategies_dir)
        self._use_sandbox = use_sandbox

    def load_strategy(self, strategy_name: str) -> Any | None:
        with _tracer.start_as_current_span("registry.load_strategy") as span:
            span.set_attribute("strategy_name", strategy_name)
            try:
                entry = self._strategies.get(strategy_name)
                if entry is None:
                    logger.warning("strategy_not_found", strategy=strategy_name)
                    return None
                try:
                    cls = load_strategy_class(entry["module_path"])
                except (ImportError, AttributeError) as exc:
                    logger.exception(
                        "strategy_load_failed",
                        strategy=strategy_name,
                        error=str(exc),
                    )
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
            except Exception as exc:
                span.set_status(trace.StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                raise

    def _verify_integrity(self, strategy_name: str, entry: dict[str, Any]) -> bool:
        manifest_data = entry.get("manifest", {})
        content_hash = manifest_data.get("content_hash")
        if not content_hash:
            return True
        module_path = entry.get("module_path")
        if not module_path:
            return False
        if not PluginSigner.verify_hash(module_path, content_hash):
            logger.error(
                "strategy_integrity_check_failed",
                strategy=strategy_name,
                expected_hash=content_hash,
            )
            return False
        logger.info("strategy_integrity_verified", strategy=strategy_name)
        return True

    def _load_sandboxed(self, strategy_name: str, cls: Any, entry: dict[str, Any]) -> Any | None:
        from engine.plugins.manifest import StrategyManifest
        from engine.plugins.sandbox import PluginSandboxExecutor, SandboxPolicy

        if not self._verify_integrity(strategy_name, entry):
            logger.error("strategy_load_aborted_integrity", strategy=strategy_name)
            return None

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
            return PluginSandboxExecutor.from_factory(cls, policy)
        except Exception as exc:
            logger.exception(
                "sandboxed_load_failed",
                strategy=strategy_name,
                error=str(exc),
            )
            return None

    def list_strategies(self) -> list[str]:
        return list(self._strategies.keys())
