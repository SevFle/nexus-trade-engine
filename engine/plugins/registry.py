"""
Plugin Registry — discovers, loads, validates, and manages strategy plugins.

Supports hot-reload, version management, and marketplace integration.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

import yaml
import structlog

from plugins.sdk import IStrategy, StrategyConfig
from plugins.manifest import StrategyManifest

logger = structlog.get_logger()


class PluginEntry:
    """A registered strategy plugin with its metadata."""

    def __init__(self, manifest: StrategyManifest, strategy_class: type, path: Path):
        self.manifest = manifest
        self.strategy_class = strategy_class
        self.path = path
        self.instance: Optional[IStrategy] = None
        self.is_loaded = False
        self.load_error: Optional[str] = None

    async def instantiate(self, config: StrategyConfig) -> IStrategy:
        """Create and initialize a strategy instance."""
        instance = self.strategy_class()
        await instance.initialize(config)
        self.instance = instance
        self.is_loaded = True
        return instance

    async def teardown(self):
        if self.instance:
            await self.instance.dispose()
            self.instance = None
            self.is_loaded = False


class PluginRegistry:
    """
    Discovers strategy plugins from the filesystem and manages their lifecycle.
    """

    def __init__(self, plugin_dir: str = "./strategies"):
        self.plugin_dir = Path(plugin_dir)
        self.plugins: dict[str, PluginEntry] = {}

    async def discover_and_load(self) -> int:
        """
        Scan plugin directory for strategy modules.
        Each strategy must have: strategy.manifest.yaml + strategy.py
        """
        if not self.plugin_dir.exists():
            logger.warning("plugin_registry.dir_not_found", path=str(self.plugin_dir))
            return 0

        count = 0
        for strategy_dir in self.plugin_dir.rglob("strategy.manifest.yaml"):
            try:
                entry = self._load_plugin(strategy_dir.parent)
                if entry:
                    self.plugins[entry.manifest.id] = entry
                    count += 1
                    logger.info(
                        "plugin_registry.loaded",
                        id=entry.manifest.id,
                        name=entry.manifest.name,
                        version=entry.manifest.version,
                    )
            except Exception as e:
                logger.error("plugin_registry.load_error", path=str(strategy_dir), error=str(e))

        return count

    def _load_plugin(self, plugin_path: Path) -> Optional[PluginEntry]:
        """Load a single plugin from a directory."""
        # Load manifest
        manifest_path = plugin_path / "strategy.manifest.yaml"
        with open(manifest_path, "r") as f:
            manifest_data = yaml.safe_load(f)
        manifest = StrategyManifest(**manifest_data)

        # Load strategy module
        strategy_path = plugin_path / "strategy.py"
        if not strategy_path.exists():
            raise FileNotFoundError(f"strategy.py not found in {plugin_path}")

        spec = importlib.util.spec_from_file_location(
            f"strategy_{manifest.id}",
            str(strategy_path),
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        # Find the IStrategy subclass
        strategy_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, IStrategy)
                and attr is not IStrategy
            ):
                strategy_class = attr
                break

        if strategy_class is None:
            raise ValueError(f"No IStrategy subclass found in {strategy_path}")

        return PluginEntry(manifest=manifest, strategy_class=strategy_class, path=plugin_path)

    def get(self, strategy_id: str) -> Optional[PluginEntry]:
        return self.plugins.get(strategy_id)

    def list_all(self) -> list[dict]:
        return [
            {
                "id": entry.manifest.id,
                "name": entry.manifest.name,
                "version": entry.manifest.version,
                "author": entry.manifest.author,
                "description": entry.manifest.description,
                "is_loaded": entry.is_loaded,
                "category": entry.manifest.marketplace.get("category", "uncategorized")
                if entry.manifest.marketplace
                else "uncategorized",
            }
            for entry in self.plugins.values()
        ]

    async def unload(self, strategy_id: str):
        entry = self.plugins.get(strategy_id)
        if entry:
            await entry.teardown()
            logger.info("plugin_registry.unloaded", id=strategy_id)

    async def reload(self, strategy_id: str) -> bool:
        entry = self.plugins.get(strategy_id)
        if not entry:
            return False
        await entry.teardown()
        new_entry = self._load_plugin(entry.path)
        if new_entry:
            self.plugins[strategy_id] = new_entry
            logger.info("plugin_registry.reloaded", id=strategy_id)
            return True
        return False
