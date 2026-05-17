from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


class PluginSigner:
    @staticmethod
    def compute_hash(file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            return ""
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def verify_hash(file_path: str | Path, expected_hash: str) -> bool:
        actual = PluginSigner.compute_hash(file_path)
        if not actual:
            return False
        return hmac.compare_digest(actual, expected_hash)

    @staticmethod
    def compute_manifest_hash(manifest_data: dict[str, Any]) -> str:
        canonical = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def compute_batch_hash(file_paths: list[str | Path]) -> str:
        h = hashlib.sha256()
        for fp in sorted(str(p) for p in file_paths):
            path = Path(fp)
            if path.exists():
                h.update(fp.encode())
                h.update(PluginSigner.compute_hash(fp).encode())
        return h.hexdigest()

    @staticmethod
    def verify_batch(file_paths: list[str | Path], expected_hash: str) -> bool:
        actual = PluginSigner.compute_batch_hash(file_paths)
        return hmac.compare_digest(actual, expected_hash)


class SignatureVerifier:
    def __init__(self, trusted_keys: dict[str, str] | None = None) -> None:
        self._trusted_keys: dict[str, str] = trusted_keys or {}
        self._verified_plugins: dict[str, dict[str, Any]] = {}

    def add_trusted_key(self, key_id: str, public_key_hex: str) -> None:
        self._trusted_keys[key_id] = public_key_hex

    def remove_key(self, key_id: str) -> None:
        self._trusted_keys.pop(key_id, None)
        self._verified_plugins.pop(key_id, None)

    def verify_plugin(
        self,
        plugin_id: str,
        file_path: str | Path,
        expected_hash: str,
        key_id: str | None = None,
    ) -> bool:
        if not PluginSigner.verify_hash(file_path, expected_hash):
            logger.warning(
                "signing.hash_mismatch",
                plugin_id=plugin_id,
                path=str(file_path),
            )
            return False

        self._verified_plugins[plugin_id] = {
            "plugin_id": plugin_id,
            "file_path": str(file_path),
            "hash": expected_hash,
            "key_id": key_id,
        }

        logger.info(
            "signing.plugin_verified",
            plugin_id=plugin_id,
            key_id=key_id,
        )
        return True

    def verify_manifest_integrity(
        self,
        manifest_data: dict[str, Any],
        expected_hash: str,
    ) -> bool:
        actual = PluginSigner.compute_manifest_hash(manifest_data)
        return hmac.compare_digest(actual, expected_hash)

    def is_plugin_verified(self, plugin_id: str) -> bool:
        return plugin_id in self._verified_plugins

    def get_verified_plugins(self) -> list[str]:
        return list(self._verified_plugins.keys())

    def revoke_plugin(self, plugin_id: str) -> None:
        self._verified_plugins.pop(plugin_id, None)
        logger.info("signing.plugin_revoked", plugin_id=plugin_id)

    def clear_verified(self) -> None:
        self._verified_plugins.clear()
