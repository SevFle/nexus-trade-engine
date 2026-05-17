"""Tests for plugin signing and integrity verification."""

from __future__ import annotations

from pathlib import Path

from engine.plugins.plugin_signing import PluginSigner, SignatureVerifier


class TestPluginSignerComputeHash:
    def test_compute_hash_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        h = PluginSigner.compute_hash(str(f))
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_compute_hash_nonexistent_file(self) -> None:
        h = PluginSigner.compute_hash("/nonexistent/file.py")
        assert h == ""

    def test_compute_hash_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("deterministic content")
        h1 = PluginSigner.compute_hash(str(f))
        h2 = PluginSigner.compute_hash(str(f))
        assert h1 == h2

    def test_compute_hash_different_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        assert PluginSigner.compute_hash(str(f1)) != PluginSigner.compute_hash(str(f2))


class TestPluginSignerVerifyHash:
    def test_verify_correct_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("test content")
        expected = PluginSigner.compute_hash(str(f))
        assert PluginSigner.verify_hash(str(f), expected) is True

    def test_verify_wrong_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("test content")
        assert PluginSigner.verify_hash(str(f), "0" * 64) is False

    def test_verify_nonexistent_file(self) -> None:
        assert PluginSigner.verify_hash("/nonexistent.py", "any") is False

    def test_verify_tampered_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("original")
        expected = PluginSigner.compute_hash(str(f))
        f.write_text("tampered")
        assert PluginSigner.verify_hash(str(f), expected) is False


class TestPluginSignerManifestHash:
    def test_compute_manifest_hash(self) -> None:
        data = {"id": "test", "version": "1.0.0"}
        h = PluginSigner.compute_manifest_hash(data)
        assert len(h) == 64

    def test_manifest_hash_deterministic(self) -> None:
        data = {"id": "test", "version": "1.0.0"}
        h1 = PluginSigner.compute_manifest_hash(data)
        h2 = PluginSigner.compute_manifest_hash(data)
        assert h1 == h2

    def test_manifest_hash_order_independent(self) -> None:
        d1 = {"a": "1", "b": "2"}
        d2 = {"b": "2", "a": "1"}
        assert PluginSigner.compute_manifest_hash(d1) == PluginSigner.compute_manifest_hash(d2)

    def test_manifest_hash_content_dependent(self) -> None:
        d1 = {"id": "test1"}
        d2 = {"id": "test2"}
        assert PluginSigner.compute_manifest_hash(d1) != PluginSigner.compute_manifest_hash(d2)


class TestPluginSignerBatchHash:
    def test_compute_batch_hash(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        h = PluginSigner.compute_batch_hash([str(f1), str(f2)])
        assert len(h) == 64

    def test_batch_hash_deterministic(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        h1 = PluginSigner.compute_batch_hash([str(f1), str(f2)])
        h2 = PluginSigner.compute_batch_hash([str(f2), str(f1)])
        assert h1 == h2

    def test_batch_hash_order_independent(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        h1 = PluginSigner.compute_batch_hash([str(f1), str(f2)])
        h2 = PluginSigner.compute_batch_hash([str(f2), str(f1)])
        assert h1 == h2

    def test_verify_batch_correct(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        expected = PluginSigner.compute_batch_hash([str(f1), str(f2)])
        assert PluginSigner.verify_batch([str(f1), str(f2)], expected) is True

    def test_verify_batch_tampered(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        expected = PluginSigner.compute_batch_hash([str(f1), str(f2)])
        f1.write_text("tampered")
        assert PluginSigner.verify_batch([str(f1), str(f2)], expected) is False


class TestSignatureVerifier:
    def test_init_empty(self) -> None:
        verifier = SignatureVerifier()
        assert verifier.get_verified_plugins() == []

    def test_init_with_keys(self) -> None:
        verifier = SignatureVerifier(trusted_keys={"key1": "abc123"})
        assert "key1" in verifier._trusted_keys

    def test_add_trusted_key(self) -> None:
        verifier = SignatureVerifier()
        verifier.add_trusted_key("test_key", "hex123")
        assert "test_key" in verifier._trusted_keys

    def test_remove_key(self) -> None:
        verifier = SignatureVerifier()
        verifier.add_trusted_key("test_key", "hex123")
        verifier.remove_key("test_key")
        assert "test_key" not in verifier._trusted_keys

    def test_verify_plugin_correct_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.py"
        f.write_text("plugin code")
        verifier = SignatureVerifier()
        h = PluginSigner.compute_hash(str(f))
        result = verifier.verify_plugin("test_plugin", str(f), h)
        assert result is True
        assert verifier.is_plugin_verified("test_plugin")

    def test_verify_plugin_wrong_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.py"
        f.write_text("plugin code")
        verifier = SignatureVerifier()
        result = verifier.verify_plugin("test_plugin", str(f), "wrong_hash")
        assert result is False
        assert not verifier.is_plugin_verified("test_plugin")

    def test_verify_plugin_with_key_id(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.py"
        f.write_text("plugin code")
        verifier = SignatureVerifier()
        h = PluginSigner.compute_hash(str(f))
        result = verifier.verify_plugin("test_plugin", str(f), h, key_id="key1")
        assert result is True

    def test_revoke_plugin(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.py"
        f.write_text("plugin code")
        verifier = SignatureVerifier()
        h = PluginSigner.compute_hash(str(f))
        verifier.verify_plugin("test_plugin", str(f), h)
        assert verifier.is_plugin_verified("test_plugin")
        verifier.revoke_plugin("test_plugin")
        assert not verifier.is_plugin_verified("test_plugin")

    def test_clear_verified(self, tmp_path: Path) -> None:
        f = tmp_path / "plugin.py"
        f.write_text("plugin code")
        verifier = SignatureVerifier()
        h = PluginSigner.compute_hash(str(f))
        verifier.verify_plugin("p1", str(f), h)
        verifier.verify_plugin("p2", str(f), h)
        assert len(verifier.get_verified_plugins()) == 2
        verifier.clear_verified()
        assert len(verifier.get_verified_plugins()) == 0

    def test_verify_manifest_integrity(self) -> None:
        data = {"id": "test", "version": "1.0.0"}
        expected = PluginSigner.compute_manifest_hash(data)
        verifier = SignatureVerifier()
        assert verifier.verify_manifest_integrity(data, expected) is True

    def test_verify_manifest_integrity_wrong(self) -> None:
        data = {"id": "test", "version": "1.0.0"}
        verifier = SignatureVerifier()
        assert verifier.verify_manifest_integrity(data, "wrong") is False

    def test_verify_manifest_detects_tampering(self) -> None:
        data = {"id": "test", "version": "1.0.0"}
        expected = PluginSigner.compute_manifest_hash(data)
        verifier = SignatureVerifier()
        tampered = {"id": "test", "version": "2.0.0"}
        assert verifier.verify_manifest_integrity(tampered, expected) is False
