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


class PluginError(Exception):
    """Raised when a strategy plugin fails validation or registration.

    Raised by :meth:`PluginRegistry.register` (and other validation entry
    points) when a candidate object is not a *concrete*
    :class:`~nexus_sdk.strategy.IStrategy` subclass — for example when an
    abstract base class such as ``IStrategy`` itself is offered for
    registration. Catching this lets callers distinguish "bad plugin" from
    ordinary ``ImportError``/``AttributeError`` coming from disk-based
    loading.
    """


def _is_strategy_class(strategy_class: Any) -> bool:
    """Return ``True`` iff *strategy_class* is a concrete IStrategy subclass.

    A candidate is accepted only when **all** of the following hold:

    1. it is itself a ``type`` (rejecting instances and arbitrary objects),
    2. it is a subclass of :class:`nexus_sdk.strategy.IStrategy`, and
    3. it has no remaining abstract methods (``__abstractmethods__`` is
       empty), i.e. it is *concrete* and can be instantiated.

    The third check is what stops the abstract base classes themselves —
    notably ``IStrategy`` — from being accepted: ``issubclass(IStrategy,
    IStrategy)`` is trivially ``True``, yet ``IStrategy()`` raises
    :class:`TypeError` because its abstract methods are unimplemented.
    Failing early here yields a clear :class:`PluginError` instead of a
    confusing ``TypeError`` deep inside instantiation.
    """
    if not isinstance(strategy_class, type):
        return False
    try:
        from nexus_sdk.strategy import IStrategy
    except ImportError:
        return False
    if not issubclass(strategy_class, IStrategy):
        return False
    # After the issubclass check: reject classes that still declare abstract
    # methods. ``__abstractmethods__`` is populated by ``ABCMeta`` at class
    # creation; an empty frozenset means every abstract method has been
    # overridden and the class is concrete.
    if getattr(strategy_class, "__abstractmethods__", set()):  # noqa: SIM103
        return False
    return True


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
    # the module body executes.  This fails fast and side-effect free,
    # complementing the runtime :class:`RestrictedImporter` hooks.  Reading
    # the file here is safe: restrictions are not active while the host loads
    # strategies, so plain ``open`` is used.
    #
    # The file is read **once** (as bytes) and the *exact same* bytes that
    # pass validation are then :func:`compile`-d and :func:`exec`-d directly
    # into the module namespace.  This closes a time-of-check/time-of-use gap:
    # ``spec.loader.exec_module`` re-reads the file from disk, so an attacker
    # who swaps the file between the validation read and the exec read could
    # execute different (un-validated) bytes than the ones just checked.
    with open(module_path, "rb") as f:
        source_bytes = f.read()
    violations = ImportValidator(DENYLIST_MODULES).validate(source_bytes)
    if violations:
        joined = "; ".join(violations)
        logger.warning(
            "strategy_source_blocked",
            path=module_path,
            violations=violations,
        )
        raise ImportError(f"Strategy source {module_path} rejected by import validator: {joined}")

    module = importlib.util.module_from_spec(spec)
    # Compile the validated bytes into a code object bound to the module's
    # file path (so tracebacks point at ``strategy.py``) and exec it directly
    # into the module's namespace.  ``spec.loader.exec_module`` is deliberately
    # avoided because it re-reads the file, which would re-open the TOCTOU gap
    # closed above; the ``module_from_spec`` call already populated
    # ``__file__``/``__loader__``/``__spec__``.
    code = compile(source_bytes, module_path, "exec")
    # ``exec`` is required (not ``spec.loader.exec_module``) so we run the
    # exact bytes that passed validation above; see the TOCTOU note.  S102 is
    # intentionally suppressed for this deliberate, audited call site.
    exec(code, module.__dict__)  # noqa: S102
    strategy_cls = getattr(module, "Strategy", None)
    if strategy_cls is None:
        logger.warning("strategy_class_not_found_in_module", path=module_path)
        raise AttributeError(f"Module {module_path} does not define a 'Strategy' class")
    return strategy_cls


class PluginRegistry:
    """Discovers and instantiates strategy plugins."""

    def __init__(self, strategies_dir: Path | None = None) -> None:
        self._strategies = discover_strategies(strategies_dir)
        # In-memory concrete strategy classes registered via :meth:`register`,
        # keyed by the registered name. These take precedence over on-disk
        # entries of the same name and let callers plug in strategies without
        # a manifest or ``strategy.py`` file.
        self._classes: dict[str, Any] = {}

    def register(self, strategy_class: Any, *, name: str | None = None) -> str:
        """Register an in-memory concrete strategy *class*.

        Unlike :meth:`load_strategy` (which reads ``strategy.py`` from disk),
        this validates and stores an already-imported class so it can be
        instantiated by name without a manifest or on-disk module.

        Parameters
        ----------
        strategy_class:
            A *concrete* :class:`~nexus_sdk.strategy.IStrategy` subclass.
        name:
            Optional registration key; defaults to ``strategy_class.__name__``.

        Returns the name under which the class was registered. Raises
        :class:`PluginError` if *strategy_class* is not a concrete
        ``IStrategy`` subclass — for example ``IStrategy`` itself (which is
        abstract) or a plain object.
        """
        if not _is_strategy_class(strategy_class):
            label = getattr(strategy_class, "__name__", strategy_class)
            abstract = sorted(getattr(strategy_class, "__abstractmethods__", set()))
            reason = (
                f"has unimplemented abstract methods {abstract}"
                if abstract
                else "is not an IStrategy subclass"
            )
            raise PluginError(
                f"{label!r} is not a concrete IStrategy subclass ({reason}) "
                f"and cannot be registered as a plugin"
            )
        registered_name = name or strategy_class.__name__
        self._classes[registered_name] = strategy_class
        logger.info(
            "strategy_registered",
            strategy=registered_name,
            cls=strategy_class.__name__,
        )
        return registered_name

    def load_strategy(self, strategy_name: str) -> Any | None:
        # In-memory registrations take precedence over on-disk entries.
        cls = self._classes.get(strategy_name)
        if cls is not None:
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
        return list({*self._strategies.keys(), *self._classes.keys()})

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
