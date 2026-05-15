"""Comprehensive tests for engine.plugins.plugin_signing — PluginSigner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from engine.plugins.plugin_signing import PluginSigner


class TestComputeHash:
    def test_returns_sha256_hex_for_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        content = b"print('hello')"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert PluginSigner.compute_hash(str(f)) == expected

    def test_accepts_path_object(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        content = b"data"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert PluginSigner.compute_hash(f) == expected

    def test_empty_file_returns_sha256_of_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.py"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert PluginSigner.compute_hash(f) == expected

    def test_nonexistent_file_returns_empty_string(self) -> None:
        result = PluginSigner.compute_hash("/nonexistent/file.py")
        assert result == ""

    def test_large_file_chunked_reading(self, tmp_path: Path) -> None:
        f = tmp_path / "large.bin"
        content = b"x" * (8192 * 3 + 42)
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert PluginSigner.compute_hash(f) == expected

    def test_binary_content_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        content = bytes(range(256))
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert PluginSigner.compute_hash(f) == expected

    def test_deterministic_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "det.py"
        f.write_text("x = 1")
        h1 = PluginSigner.compute_hash(f)
        h2 = PluginSigner.compute_hash(f)
        assert h1 == h2

    def test_hash_changes_with_content(self, tmp_path: Path) -> None:
        f = tmp_path / "mutable.py"
        f.write_text("x = 1")
        h1 = PluginSigner.compute_hash(f)
        f.write_text("x = 2")
        h2 = PluginSigner.compute_hash(f)
        assert h1 != h2


class TestVerifyHash:
    def test_correct_hash_returns_true(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        content = b"hello world"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert PluginSigner.verify_hash(f, expected) is True

    def test_wrong_hash_returns_false(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("hello")
        assert PluginSigner.verify_hash(f, "0" * 64) is False

    def test_nonexistent_file_returns_false(self) -> None:
        assert PluginSigner.verify_hash("/nonexistent.py", "abc") is False

    def test_empty_hash_returns_false(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("content")
        assert PluginSigner.verify_hash(f, "") is False


class TestComputeManifestHash:
    def test_deterministic_hash(self) -> None:
        data = {"id": "test", "version": "1.0.0", "name": "Test"}
        h1 = PluginSigner.compute_manifest_hash(data)
        h2 = PluginSigner.compute_manifest_hash(data)
        assert h1 == h2

    def test_key_order_does_not_matter(self) -> None:
        data1 = {"a": 1, "b": 2, "c": 3}
        data2 = {"c": 3, "a": 1, "b": 2}
        assert PluginSigner.compute_manifest_hash(data1) == PluginSigner.compute_manifest_hash(data2)

    def test_different_data_different_hash(self) -> None:
        data1 = {"id": "test1"}
        data2 = {"id": "test2"}
        assert PluginSigner.compute_manifest_hash(data1) != PluginSigner.compute_manifest_hash(data2)

    def test_uses_canonical_json(self) -> None:
        data = {"z": 1, "a": 2}
        expected_input = json.dumps(data, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(expected_input.encode()).hexdigest()
        assert PluginSigner.compute_manifest_hash(data) == expected

    def test_empty_dict(self) -> None:
        h = PluginSigner.compute_manifest_hash({})
        assert isinstance(h, str)
        assert len(h) == 64

    def test_nested_dict(self) -> None:
        data = {"config": {"threshold": 0.5}, "name": "test"}
        h = PluginSigner.compute_manifest_hash(data)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_hash_is_sha256_hex(self) -> None:
        h = PluginSigner.compute_manifest_hash({"key": "value"})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
