from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from app.fast_api_app import create_admission_app
from app.ledger import InMemoryEventStore
from app.live_source import BASELINE_SHA256, DRIFT_SHA256, LiveSourceWatcher

BASELINE = b"id,name\n1,Central Library\n2,Riverside Clinic\n3,North School\n"
DRIFT = b"id,full_name\n1,Central Library\n2,Riverside Clinic\n3,North School\n"
ROOT = Path(__file__).resolve().parents[2]


class BytesReader:
    def __init__(self, value: bytes) -> None:
        self.value = value

    async def read(self) -> bytes:
        return self.value


class RecordingAdmission:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def start(
        self,
        scenario_id: str,
        *,
        trigger: str,
        source_sha256: str,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "scenario_id": scenario_id,
                "trigger": trigger,
                "source_sha256": source_sha256,
            }
        )
        return {"id": "event-1", "scenario_id": scenario_id, "status": "queued"}


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def publish(self, **payload: str) -> None:
        self.calls.append(payload)


def test_live_source_digests_are_bound_to_the_packaged_evidence() -> None:
    before = (ROOT / "benchmark/fixtures/column-rename-before.csv").read_bytes()
    after = (ROOT / "benchmark/fixtures/column-rename-after.csv").read_bytes()

    assert hashlib.sha256(before).hexdigest() == BASELINE_SHA256
    assert hashlib.sha256(after).hexdigest() == DRIFT_SHA256


@pytest.mark.asyncio
async def test_watcher_dispatches_only_the_digest_bound_drift() -> None:
    admission = RecordingAdmission()

    stable = await LiveSourceWatcher(BytesReader(BASELINE), admission).watch()
    unknown = await LiveSourceWatcher(BytesReader(b"unrecognized"), admission).watch()
    changed = await LiveSourceWatcher(BytesReader(DRIFT), admission).watch()

    assert stable["status"] == "stable"
    assert unknown["status"] == "unsupported_change"
    assert changed["status"] == "drift_detected"
    assert changed["queue_status"] == "queued"
    assert admission.calls == [
        {
            "scenario_id": "column-rename",
            "trigger": "cloud-scheduler",
            "source_sha256": changed["source_sha256"],
        }
    ]


@pytest.mark.asyncio
async def test_authenticated_watch_endpoint_records_source_receipt() -> None:
    publisher = RecordingPublisher()
    application = create_admission_app(
        publisher=publisher,
        store=InMemoryEventStore(),
        source_reader=BytesReader(DRIFT),
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://admission") as client:
        response = await client.post("/internal/watch")

    assert response.status_code == 200
    assert response.json()["status"] == "drift_detected"
    assert publisher.calls[0]["scenario_id"] == "column-rename"
