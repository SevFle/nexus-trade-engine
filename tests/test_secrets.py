"""Tests for engine.core.secrets — encrypted secret storage with rotation."""

from __future__ import annotations

import pytest

from engine.core.secrets import (
    InMemorySecretStore,
    MasterKey,
    SecretsError,
    SecretsService,
    generate_master_key,
)


@pytest.fixture
def master() -> MasterKey:
    return MasterKey(current=generate_master_key())


@pytest.fixture
def service(master: MasterKey) -> SecretsService:
    return SecretsService(store=InMemorySecretStore(), master_key=master)


class TestPutGet:
    @pytest.mark.asyncio
    async def test_round_trip(self, service: SecretsService) -> None:
        await service.put("api_key", "s3cret-value")
        out = await service.get("api_key")
        assert out == "s3cret-value"

    @pytest.mark.asyncio
    async def test_overwrite_replaces_value(self, service: SecretsService) -> None:
        await service.put("k", "v1")
        await service.put("k", "v2")
        assert await service.get("k") == "v2"

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, service: SecretsService) -> None:
        assert await service.get("nope") is None

    @pytest.mark.asyncio
    async def test_ciphertext_is_not_plaintext(self, service: SecretsService) -> None:
        await service.put("k", "plaintext-marker")
        rec = await service.store.get_record("k")
        assert rec is not None
        assert b"plaintext-marker" not in rec.ciphertext

    @pytest.mark.asyncio
    async def test_empty_name_rejected(self, service: SecretsService) -> None:
        with pytest.raises(SecretsError):
            await service.put("", "v")

    @pytest.mark.asyncio
    async def test_whitespace_only_name_rejected(
        self, service: SecretsService
    ) -> None:
        with pytest.raises(SecretsError):
            await service.put("   ", "v")

    @pytest.mark.asyncio
    async def test_empty_value_rejected(self, service: SecretsService) -> None:
        with pytest.raises(SecretsError):
            await service.put("k", "")

    @pytest.mark.asyncio
    async def test_name_normalized_consistently(
        self, service: SecretsService
    ) -> None:
        await service.put("  api_key  ", "v")
        assert await service.get("api_key") == "v"
        assert await service.get("  api_key  ") == "v"

    @pytest.mark.asyncio
    async def test_unicode_name_round_trip(self, service: SecretsService) -> None:
        await service.put("密钥", "v")
        assert await service.get("密钥") == "v"

    @pytest.mark.asyncio
    async def test_corrupted_ciphertext_raises(
        self, service: SecretsService
    ) -> None:
        from engine.core.secrets import SecretRecord

        await service.store.put_record(
            SecretRecord(
                name="garbage",
                ciphertext=b"not-a-fernet-token",
                created_at_epoch=0.0,
                updated_at_epoch=0.0,
            )
        )
        with pytest.raises(SecretsError):
            await service.get("garbage")

    @pytest.mark.asyncio
    async def test_get_whitespace_only_name_rejected(
        self, service: SecretsService
    ) -> None:
        with pytest.raises(SecretsError):
            await service.get("   ")


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_secret(self, service: SecretsService) -> None:
        await service.put("k", "v")
        await service.delete("k")
        assert await service.get("k") is None

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self, service: SecretsService) -> None:
        await service.delete("never-existed")

    @pytest.mark.asyncio
    async def test_delete_whitespace_only_name_rejected(
        self, service: SecretsService
    ) -> None:
        with pytest.raises(SecretsError):
            await service.delete("   ")


class TestList:
    @pytest.mark.asyncio
    async def test_list_returns_names_only(self, service: SecretsService) -> None:
        await service.put("a", "x")
        await service.put("b", "y")
        names = await service.list_names()
        assert set(names) == {"a", "b"}


class TestRotation:
    @pytest.mark.asyncio
    async def test_old_secrets_decrypt_after_rotation_with_previous(
        self, service: SecretsService
    ) -> None:
        await service.put("k", "v")
        new_key = generate_master_key()
        service.rotate_master_key(new_current=new_key)
        assert await service.get("k") == "v"

    @pytest.mark.asyncio
    async def test_reencrypt_all_uses_new_key(
        self, service: SecretsService
    ) -> None:
        await service.put("k", "v")
        old_record = await service.store.get_record("k")
        assert old_record is not None
        old_ct = old_record.ciphertext
        new_key = generate_master_key()
        service.rotate_master_key(new_current=new_key)
        await service.reencrypt_all()
        new_record = await service.store.get_record("k")
        assert new_record is not None
        assert new_record.ciphertext != old_ct
        assert await service.get("k") == "v"

    @pytest.mark.asyncio
    async def test_drop_previous_key_after_reencrypt(
        self, service: SecretsService
    ) -> None:
        await service.put("k", "v")
        new_key = generate_master_key()
        service.rotate_master_key(new_current=new_key)
        await service.reencrypt_all()
        service.drop_previous_key()
        assert await service.get("k") == "v"

    @pytest.mark.asyncio
    async def test_decrypt_fails_without_either_key(self) -> None:
        store = InMemorySecretStore()
        svc1 = SecretsService(
            store=store, master_key=MasterKey(current=generate_master_key())
        )
        await svc1.put("k", "v")
        svc2 = SecretsService(
            store=store, master_key=MasterKey(current=generate_master_key())
        )
        with pytest.raises(SecretsError):
            await svc2.get("k")

    @pytest.mark.asyncio
    async def test_double_rotation_without_reencrypt_rejected(
        self, service: SecretsService
    ) -> None:
        service.rotate_master_key(new_current=generate_master_key())
        with pytest.raises(SecretsError, match="previous key is still held"):
            service.rotate_master_key(new_current=generate_master_key())

    @pytest.mark.asyncio
    async def test_reencrypt_all_aborts_on_undecryptable_record(self) -> None:
        from engine.core.secrets import SecretRecord

        store = InMemorySecretStore()
        svc = SecretsService(
            store=store, master_key=MasterKey(current=generate_master_key())
        )
        await svc.put("good", "v")
        # Insert a record that decrypts under neither key.
        await store.put_record(
            SecretRecord(
                name="bad",
                ciphertext=b"not-a-fernet-token",
                created_at_epoch=0.0,
                updated_at_epoch=0.0,
            )
        )
        good_before = await store.get_record("good")
        assert good_before is not None
        new_key = generate_master_key()
        svc.rotate_master_key(new_current=new_key)
        with pytest.raises(SecretsError, match="No records were modified"):
            await svc.reencrypt_all()
        # Good record must be untouched (atomic abort).
        good_after = await store.get_record("good")
        assert good_after is not None
        assert good_after.ciphertext == good_before.ciphertext

    @pytest.mark.asyncio
    async def test_reencrypt_error_does_not_leak_names(self) -> None:
        from engine.core.secrets import SecretRecord

        store = InMemorySecretStore()
        svc = SecretsService(
            store=store, master_key=MasterKey(current=generate_master_key())
        )
        await store.put_record(
            SecretRecord(
                name="prod_stripe_key",
                ciphertext=b"not-a-fernet-token",
                created_at_epoch=0.0,
                updated_at_epoch=0.0,
            )
        )
        with pytest.raises(SecretsError) as exc_info:
            await svc.reencrypt_all()
        assert "prod_stripe_key" not in str(exc_info.value)


class TestMasterKey:
    def test_generate_master_key_returns_bytes(self) -> None:
        k = generate_master_key()
        assert isinstance(k, bytes)
        assert len(k) > 0

    def test_two_generated_keys_differ(self) -> None:
        assert generate_master_key() != generate_master_key()

    def test_master_key_rejects_empty_current(self) -> None:
        with pytest.raises(SecretsError):
            MasterKey(current=b"")

    def test_master_key_rejects_short_key(self) -> None:
        with pytest.raises(SecretsError, match="32 bytes"):
            MasterKey(current=b"tooshort")

    def test_master_key_rejects_non_base64_key(self) -> None:
        # 32 random raw bytes — not url-safe-base64-encoded.
        raw_bytes = bytes(range(32))
        with pytest.raises(SecretsError):
            MasterKey(current=raw_bytes)

    def test_master_key_rejects_invalid_previous(self) -> None:
        with pytest.raises(SecretsError):
            MasterKey(current=generate_master_key(), previous=b"garbage")

    def test_rotate_rejects_invalid_new_key(
        self, service: SecretsService
    ) -> None:
        with pytest.raises(SecretsError):
            service.rotate_master_key(new_current=b"")

    def test_rotate_rejects_short_new_key(
        self, service: SecretsService
    ) -> None:
        with pytest.raises(SecretsError, match="32 bytes"):
            service.rotate_master_key(new_current=b"tooshort")
