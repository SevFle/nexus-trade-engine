from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType
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


def _read_strategy_source(module_path: str) -> str:
    """Read strategy source from disk **exactly once**.

    Returning the source as an in-memory string here — instead of letting
    ``importlib`` re-read the file at execution time — is what closes the
    time-of-check-to-time-of-use (TOCTOU) window in
    :func:`load_strategy_class`: the bytes that are statically validated are
    the *exact* bytes that are compiled and executed.  An ``OSError`` (missing
    file, permission denied, …) is normalised to ``ImportError`` so callers
    keep their existing ``except ImportError`` handling.
    """
    try:
        return Path(module_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ImportError(f"Cannot read strategy source from {module_path}: {exc}") from exc


def _validate_source(source: str, *, module_path: str) -> str:
    """Statically validate strategy source; return the string that will run.

    The returned string is compiled and executed **unchanged**, so callers are
    guaranteed that *what was validated is what runs* — there is no second disk
    read between validation and execution.  Currently this parses the source
    into an AST purely so a ``SyntaxError`` is reported with the strategy's
    real file path (mirroring ``spec.loader.exec_module``'s behaviour).
    Additional static checks (forbidden imports, dangerous calls, …) belong
    here; layered here they are guaranteed to see the exact bytes that execute.
    """
    ast.parse(source, filename=module_path)
    return source


def load_strategy_class(module_path: str) -> Any:
    # A spec is still created so we fail fast on paths the import machinery
    # cannot locate, and so the loaded module carries ``__spec__``/``__file__``
    # metadata.  Critically, the spec's loader is **never** invoked: that would
    # re-read the file from disk and reintroduce the TOCTOU gap.
    spec = importlib.util.spec_from_file_location("strategy", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load strategy from {module_path}")

    # ── TOCTOU-safe load: read once → validate → compile → exec ─────────
    # ``spec.loader.exec_module`` (the previous implementation) reads the file
    # from disk a *second* time at execution, so the bytes that were validated
    # could differ from the bytes that actually run if the file changed
    # between the two reads.  Instead we read the source once, statically
    # validate it, compile the *validated* string into a code object, and exec
    # that code object directly — guaranteeing validated bytes == executed
    # bytes.
    source = _read_strategy_source(module_path)
    validated = _validate_source(source, module_path=module_path)
    code = compile(validated, module_path, "exec")  # validated source → code object

    module = ModuleType("strategy")
    module.__file__ = module_path
    module.__spec__ = spec
    # Execute the *already-validated* code object directly.  No second disk
    # read occurs here, closing the TOCTOU window.
    exec(code, module.__dict__)  # noqa: S102 — runs the validated code object

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
