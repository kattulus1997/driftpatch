from __future__ import annotations

import json

import pytest

from app.bundle_store import (
    BundleIntegrityError,
    BundleMissing,
    CloudStorageBundleStore,
    InMemoryBundleStore,
)
from app.schemas import CustomRunSubmission, SourceDocument


def _submission() -> CustomRunSubmission:
    return CustomRunSubmission(
        label="Supplier orders",
        before=SourceDocument(format="csv", content="id,total\n1,10\n"),
        after=SourceDocument(format="json", content='[{"id":1,"total":10}]'),
        pipeline_json='{"format":"csv","fields":{"id":"id","total":"total"}}',
        contract_json=(
            '{"required":["id","total"],'
            '"types":{"id":"integer","total":"integer"},"unique_key":"id"}'
        ),
    )


@pytest.mark.asyncio
async def test_bundle_round_trip_verifies_generation_and_digest() -> None:
    submission = _submission()
    store = InMemoryBundleStore()

    stored = await store.put("custom_abc", submission)

    assert stored.object_name == "custom/custom_abc.json"
    assert stored.generation == 1
    assert stored.size_bytes > 0
    assert await store.get(stored) == submission


@pytest.mark.asyncio
async def test_delete_is_idempotent_for_the_exact_generation() -> None:
    store = InMemoryBundleStore()
    stored = await store.put("custom_delete", _submission())

    await store.delete(stored)
    await store.delete(stored)

    with pytest.raises(BundleMissing):
        await store.get(stored)


@pytest.mark.asyncio
async def test_digest_or_generation_mismatch_never_returns_content() -> None:
    store = InMemoryBundleStore()
    stored = await store.put("custom_integrity", _submission())

    with pytest.raises(BundleIntegrityError, match="digest"):
        await store.get(stored.model_copy(update={"sha256": "0" * 64}))
    with pytest.raises(BundleMissing, match="generation"):
        await store.get(stored.model_copy(update={"generation": 2}))


class _Blob:
    def __init__(self, name: str, generation: int | None, payload: bytes):
        self.name = name
        self.generation = generation
        self.payload = payload
        self.upload_kwargs: dict | None = None
        self.download_kwargs: dict | None = None
        self.delete_kwargs: dict | None = None

    def upload_from_string(self, value: bytes, **kwargs) -> None:
        self.payload = value
        self.upload_kwargs = kwargs
        self.generation = 73

    def download_as_bytes(self, **kwargs) -> bytes:
        self.download_kwargs = kwargs
        return self.payload

    def delete(self, **kwargs) -> None:
        self.delete_kwargs = kwargs


class _Bucket:
    def __init__(self):
        self.payload = b""
        self.blobs: list[_Blob] = []

    def blob(self, name: str, generation: int | None = None) -> _Blob:
        blob = _Blob(name, generation, self.payload)
        self.blobs.append(blob)
        return blob


class _Client:
    def __init__(self):
        self.bucket_instance = _Bucket()

    def bucket(self, _name: str) -> _Bucket:
        return self.bucket_instance


@pytest.mark.asyncio
async def test_cloud_store_uses_create_only_and_exact_generation_preconditions() -> None:
    client = _Client()
    store = CloudStorageBundleStore("private-bundles", client=client)

    stored = await store.put("custom_cloud", _submission())
    upload = client.bucket_instance.blobs[-1]
    client.bucket_instance.payload = upload.payload
    loaded = await store.get(stored)
    download = client.bucket_instance.blobs[-1]
    await store.delete(stored)
    deleted = client.bucket_instance.blobs[-1]

    assert loaded == _submission()
    assert upload.upload_kwargs == {
        "content_type": "application/json",
        "if_generation_match": 0,
    }
    assert download.generation == 73
    assert download.download_kwargs == {"if_generation_match": 73}
    assert deleted.generation == 73
    assert deleted.delete_kwargs == {"if_generation_match": 73}
    assert json.loads(upload.payload)["label"] == "Supplier orders"
