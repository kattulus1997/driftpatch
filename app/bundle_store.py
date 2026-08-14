from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import storage

from .case_data import MAX_REQUEST_BYTES
from .schemas import CustomRunSubmission, StoredBundle

_RUN_ID = re.compile(r"^[a-z0-9_]{1,120}$")


class BundleError(RuntimeError):
    pass


class BundleExists(BundleError):
    pass


class BundleMissing(BundleError):
    pass


class BundleIntegrityError(BundleError):
    pass


class BundleConfigurationError(BundleError):
    pass


class BundleStore(Protocol):
    async def put(
        self, run_id: str, value: CustomRunSubmission
    ) -> StoredBundle: ...

    async def get(self, reference: StoredBundle) -> CustomRunSubmission: ...

    async def delete(self, reference: StoredBundle) -> None: ...


def _canonical_bytes(value: CustomRunSubmission) -> bytes:
    payload = json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise BundleIntegrityError("bundle size is outside the accepted envelope")
    return payload


def _object_name(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid custom run identifier")
    return f"custom/{run_id}.json"


def _reference(object_name: str, generation: int, payload: bytes) -> StoredBundle:
    return StoredBundle(
        object_name=object_name,
        generation=generation,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _decode(reference: StoredBundle, payload: bytes) -> CustomRunSubmission:
    if len(payload) != reference.size_bytes:
        raise BundleIntegrityError("bundle size mismatch")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != reference.sha256:
        raise BundleIntegrityError("bundle digest mismatch")
    try:
        return CustomRunSubmission.model_validate_json(payload)
    except ValueError as exc:
        raise BundleIntegrityError("bundle schema validation failed") from exc


class InMemoryBundleStore:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[int, bytes]] = {}
        self._next_generation = 1
        self._lock = asyncio.Lock()

    async def put(
        self, run_id: str, value: CustomRunSubmission
    ) -> StoredBundle:
        object_name = _object_name(run_id)
        payload = _canonical_bytes(value)
        async with self._lock:
            if object_name in self._objects:
                raise BundleExists("bundle object already exists")
            generation = self._next_generation
            self._next_generation += 1
            self._objects[object_name] = (generation, payload)
        return _reference(object_name, generation, payload)

    async def get(self, reference: StoredBundle) -> CustomRunSubmission:
        async with self._lock:
            stored = self._objects.get(reference.object_name)
        if stored is None:
            raise BundleMissing("bundle object is missing")
        generation, payload = stored
        if generation != reference.generation:
            raise BundleMissing("bundle generation is missing")
        return _decode(reference, payload)

    async def delete(self, reference: StoredBundle) -> None:
        async with self._lock:
            stored = self._objects.get(reference.object_name)
            if stored is None:
                return
            if stored[0] != reference.generation:
                raise BundleMissing("bundle generation is missing")
            del self._objects[reference.object_name]


class CloudStorageBundleStore:
    def __init__(
        self,
        bucket_name: str,
        *,
        client: storage.Client | None = None,
    ) -> None:
        if not bucket_name:
            raise BundleConfigurationError("custom bundle bucket is missing")
        self._bucket = (client or storage.Client()).bucket(bucket_name)

    async def put(
        self, run_id: str, value: CustomRunSubmission
    ) -> StoredBundle:
        object_name = _object_name(run_id)
        payload = _canonical_bytes(value)
        blob = self._bucket.blob(object_name)
        try:
            await asyncio.to_thread(
                blob.upload_from_string,
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
        except google_exceptions.PreconditionFailed as exc:
            raise BundleExists("bundle object already exists") from exc
        except google_exceptions.GoogleAPICallError as exc:
            raise BundleError("bundle upload failed") from exc
        if not isinstance(blob.generation, int) or blob.generation <= 0:
            raise BundleIntegrityError("bundle upload returned no generation")
        return _reference(object_name, blob.generation, payload)

    async def get(self, reference: StoredBundle) -> CustomRunSubmission:
        blob = self._bucket.blob(
            reference.object_name, generation=reference.generation
        )
        try:
            payload = await asyncio.to_thread(
                blob.download_as_bytes,
                if_generation_match=reference.generation,
            )
        except (google_exceptions.NotFound, google_exceptions.PreconditionFailed) as exc:
            raise BundleMissing("bundle generation is missing") from exc
        except google_exceptions.GoogleAPICallError as exc:
            raise BundleError("bundle download failed") from exc
        return _decode(reference, payload)

    async def delete(self, reference: StoredBundle) -> None:
        blob = self._bucket.blob(
            reference.object_name, generation=reference.generation
        )
        try:
            await asyncio.to_thread(
                blob.delete, if_generation_match=reference.generation
            )
        except google_exceptions.NotFound:
            return
        except google_exceptions.PreconditionFailed as exc:
            raise BundleMissing("bundle generation is missing") from exc
        except google_exceptions.GoogleAPICallError as exc:
            raise BundleError("bundle deletion failed") from exc


_LOCAL_STORE = InMemoryBundleStore()


def configured_bundle_store() -> BundleStore:
    if os.environ.get("K_SERVICE"):
        return CloudStorageBundleStore(os.environ.get("CUSTOM_BUNDLE_BUCKET", ""))
    return _LOCAL_STORE
