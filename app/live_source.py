from __future__ import annotations

import asyncio
import hashlib
import os
from functools import cache
from typing import Any, Protocol
from urllib.parse import quote

import google.auth
import httpx
from google.auth.transport.requests import Request

LIVE_SCENARIO_ID = "column-rename"
BASELINE_SHA256 = "3c92716578995dffefd381b2481785295036f7f02b7c647d31023711767b3da6"
DRIFT_SHA256 = "da3e209b6e97103c43bc5045fce139503d266cf7fc3041b6132c652ac376f196"
MAX_SOURCE_BYTES = 64 * 1024
STORAGE_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


class LiveSourceError(RuntimeError):
    pass


class SourceReader(Protocol):
    async def read(self) -> bytes: ...


class AdmissionStarter(Protocol):
    async def start(
        self,
        scenario_id: str,
        *,
        trigger: str,
        source_sha256: str,
    ) -> dict[str, Any]: ...


class GcsSourceReader:
    def __init__(self, bucket: str, object_name: str) -> None:
        self._bucket = bucket
        self._object_name = object_name
        self._credentials, _ = google.auth.default(scopes=[STORAGE_SCOPE])

    async def read(self) -> bytes:
        try:
            if not self._credentials.valid:
                await asyncio.to_thread(self._credentials.refresh, Request())
            url = (
                "https://storage.googleapis.com/download/storage/v1/b/"
                f"{quote(self._bucket, safe='')}/o/"
                f"{quote(self._object_name, safe='')}?alt=media"
            )
            chunks: list[bytes] = []
            size = 0
            async with httpx.AsyncClient(timeout=15) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "Authorization": f"Bearer {self._credentials.token}",
                        "Range": f"bytes=0-{MAX_SOURCE_BYTES}",
                    },
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_SOURCE_BYTES:
                            raise LiveSourceError("live source exceeds the byte budget")
                        chunks.append(chunk)
            return b"".join(chunks)
        except LiveSourceError:
            raise
        except Exception as exc:
            raise LiveSourceError("live source is unavailable") from exc


class LiveSourceWatcher:
    def __init__(self, source: SourceReader, admission: AdmissionStarter) -> None:
        self._source = source
        self._admission = admission

    async def watch(self) -> dict[str, Any]:
        observed = hashlib.sha256(await self._source.read()).hexdigest()
        if observed == BASELINE_SHA256:
            return {
                "scenario_id": LIVE_SCENARIO_ID,
                "status": "stable",
                "source_sha256": observed,
            }
        if observed != DRIFT_SHA256:
            return {
                "scenario_id": LIVE_SCENARIO_ID,
                "status": "unsupported_change",
                "source_sha256": observed,
            }
        receipt = await self._admission.start(
            LIVE_SCENARIO_ID,
            trigger="cloud-scheduler",
            source_sha256=observed,
        )
        return {
            **receipt,
            "status": "drift_detected",
            "queue_status": receipt["status"],
            "source_sha256": observed,
        }


@cache
def configured_source_reader() -> SourceReader:
    bucket = os.getenv("LIVE_SOURCE_BUCKET", "").strip()
    object_name = os.getenv("LIVE_SOURCE_OBJECT", "").strip()
    if not bucket or not object_name:
        raise LiveSourceError("live source is not configured")
    return GcsSourceReader(bucket, object_name)
