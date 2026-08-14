from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.admission import AdmissionRejected, AdmissionService
from app.bundle_store import InMemoryBundleStore
from app.event_delivery import EventDeliveryError
from app.ledger import InMemoryEventStore
from app.schemas import CustomRunSubmission, SourceDocument, TaskRequest


def _repairable(index: int = 1, *, label: str = "Orders") -> CustomRunSubmission:
    return CustomRunSubmission(
        label=label,
        before=SourceDocument(
            format="csv", content=f"id,total\n{index},{index * 10}\n"
        ),
        after=SourceDocument(
            format="json", content=f'[{{"id":{index},"total":{index * 10}}}]'
        ),
        pipeline_json=json.dumps(
            {
                "format": "csv",
                "fields": {"id": "id", "total": "total"},
                "casts": {"id": "integer", "total": "integer"},
            }
        ),
        contract_json=json.dumps(
            {
                "required": ["id", "total"],
                "types": {"id": "integer", "total": "integer"},
                "unique_key": "id",
                "preserve_values": ["total"],
            }
        ),
    )


def _unchanged(index: int) -> CustomRunSubmission:
    source = f"id,total\n{index},{index * 10}\n"
    return _repairable(index).model_copy(
        update={"after": SourceDocument(format="csv", content=source)}
    )


class CountingBundles(InMemoryBundleStore):
    def __init__(self) -> None:
        super().__init__()
        self.put_calls = 0
        self.delete_calls = 0

    async def put(self, run_id, value):
        self.put_calls += 1
        return await super().put(run_id, value)

    async def delete(self, reference):
        self.delete_calls += 1
        return await super().delete(reference)


class Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def publish(self, task: TaskRequest) -> None:
        self.calls.append(task.model_dump())
        if self.fail:
            raise EventDeliveryError("queue unavailable")


@pytest.mark.asyncio
async def test_twenty_fifth_inference_run_is_rejected_before_upload() -> None:
    store = InMemoryEventStore()
    publisher = Publisher()
    bundles = CountingBundles()
    service = AdmissionService(store, publisher, bundles, daily_custom_limit=24)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    for index in range(1, 25):
        await service.start_custom(_repairable(index), now=now)

    with pytest.raises(AdmissionRejected) as error:
        await service.start_custom(_repairable(25), now=now)

    assert error.value.status_code == 429
    assert error.value.code == "daily_limit"
    assert bundles.put_calls == 24
    assert len(publisher.calls) == 24


@pytest.mark.asyncio
async def test_unchanged_fast_paths_do_not_consume_inference_quota() -> None:
    store = InMemoryEventStore()
    publisher = Publisher()
    bundles = CountingBundles()
    service = AdmissionService(store, publisher, bundles, daily_custom_limit=1)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    for index in range(1, 8):
        await service.start_custom(_unchanged(index), now=now)
    await service.start_custom(_repairable(20), now=now)

    with pytest.raises(AdmissionRejected) as error:
        await service.start_custom(_repairable(21), now=now)

    assert error.value.status_code == 429
    assert len(publisher.calls) == 8


@pytest.mark.asyncio
async def test_unchanged_runs_still_consume_the_total_admission_budget() -> None:
    store = InMemoryEventStore()
    publisher = Publisher()
    bundles = CountingBundles()
    service = AdmissionService(
        store,
        publisher,
        bundles,
        daily_custom_limit=24,
        daily_total_limit=3,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    for index in range(1, 4):
        await service.start_custom(_unchanged(index), now=now)

    with pytest.raises(AdmissionRejected) as error:
        await service.start_custom(_unchanged(4), now=now)

    assert error.value.status_code == 429
    assert error.value.code == "daily_total_limit"
    assert bundles.put_calls == 3
    assert len(publisher.calls) == 3


@pytest.mark.asyncio
async def test_concurrent_duplicate_content_uploads_and_dispatches_once() -> None:
    store = InMemoryEventStore()
    publisher = Publisher()
    bundles = CountingBundles()
    service = AdmissionService(store, publisher, bundles)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    receipts = await asyncio.gather(
        *(
            service.start_custom(
                _repairable(label=f"Cosmetic label {index}"), now=now
            )
            for index in range(20)
        )
    )

    assert len({receipt["id"] for receipt in receipts}) == 1
    assert bundles.put_calls == 1
    assert len(publisher.calls) == 1
    assert publisher.calls[0]["case_kind"] == "custom"
    assert publisher.calls[0]["bundle"]["object_name"].startswith("custom/")


@pytest.mark.asyncio
async def test_queue_failure_rolls_back_claim_quota_and_object() -> None:
    store = InMemoryEventStore()
    bundles = CountingBundles()
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    failing = AdmissionService(
        store, Publisher(fail=True), bundles, daily_custom_limit=1
    )

    with pytest.raises(AdmissionRejected) as error:
        await failing.start_custom(_repairable(), now=now)

    assert error.value.status_code == 503
    assert bundles.put_calls == bundles.delete_calls == 1

    publisher = Publisher()
    retry = AdmissionService(store, publisher, bundles, daily_custom_limit=1)
    receipt = await retry.start_custom(_repairable(), now=now)

    assert receipt["status"] == "queued"
    assert len(publisher.calls) == 1
    assert bundles.put_calls == 2


@pytest.mark.asyncio
async def test_invalid_baseline_has_no_storage_queue_or_ledger_side_effect() -> None:
    store = InMemoryEventStore()
    publisher = Publisher()
    bundles = CountingBundles()
    service = AdmissionService(store, publisher, bundles)
    invalid = _repairable().model_copy(
        update={
            "before": SourceDocument(
                format="csv", content="id,total\n1,not-an-integer\n"
            )
        }
    )

    with pytest.raises(AdmissionRejected) as error:
        await service.start_custom(invalid)

    assert error.value.status_code == 422
    assert error.value.code == "invalid_baseline"
    assert bundles.put_calls == 0
    assert publisher.calls == []
    assert await store.list_terminal(10) == []


@pytest.mark.asyncio
async def test_ledger_failure_is_redacted_and_happens_before_upload() -> None:
    class FailingClaimStore(InMemoryEventStore):
        async def claim_custom(self, **_kwargs):
            raise RuntimeError("private Firestore detail")

    bundles = CountingBundles()
    service = AdmissionService(FailingClaimStore(), Publisher(), bundles)

    with pytest.raises(AdmissionRejected) as error:
        await service.start_custom(_repairable())

    assert error.value.status_code == 503
    assert error.value.code == "admission_unavailable"
    assert "Firestore" not in str(error.value)
    assert bundles.put_calls == 0


@pytest.mark.asyncio
async def test_custom_claim_is_atomic_under_concurrency_and_releasable() -> None:
    store = InMemoryEventStore()
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    claims = await asyncio.gather(
        *(
            store.claim_custom(
                candidate_run_id=f"custom_{index:032x}",
                digest="a" * 64,
                uses_inference=True,
                now=now,
                lease=timedelta(minutes=65),
                limit=24,
            )
            for index in range(20)
        )
    )

    acquired = [claim for claim in claims if claim.disposition == "acquired"]
    assert len(acquired) == 1
    assert {claim.run_id for claim in claims} == {acquired[0].run_id}
    assert acquired[0].token is not None
    assert await store.release_custom(
        run_id=acquired[0].run_id, claim_token=acquired[0].token
    )

    replacement = await store.claim_custom(
        candidate_run_id="custom_ffffffffffffffffffffffffffffffff",
        digest="a" * 64,
        uses_inference=True,
        now=now,
        lease=timedelta(minutes=65),
        limit=24,
    )
    assert replacement.disposition == "acquired"
