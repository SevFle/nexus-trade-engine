from __future__ import annotations

import hashlib
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
        return actual == expected_hash

    @staticmethod
    def compute_manifest_hash(manifest_data: dict[str, Any]) -> str:
        canonical = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
