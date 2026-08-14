from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.admission import (
    AdmissionError,
    AdmissionService,
    CachedAdmissionClient,
    DirectAdmissionClient,
)
from app.benchmark import load_scenario
from app.event_delivery import EventDeliveryError
from app.event_identity import daily_event_id
from app.ledger import InMemoryEventStore
from app.repairs import program_from_legacy_plan
from app.result_service import ProposalRejected, ResultService
from app.schemas import RepairPlan, TaskRequest, WorkerProposal


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def publish(self, task: TaskRequest) -> None:
        self.calls.append(task.model_dump(mode="json"))


class FailingPublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, task: TaskRequest) -> None:
        del task
        self.calls += 1
        raise EventDeliveryError("simulated queue outage")


@pytest.mark.asyncio
async def test_durable_admission_coalesces_concurrent_public_replays() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    service = AdmissionService(store, publisher)

    receipts = await asyncio.gather(
        *(service.start("column-rename") for _ in range(20))
    )

    assert len(publisher.calls) == 1
    assert len({receipt["id"] for receipt in receipts}) == 1
    assert {receipt["status"] for receipt in receipts} == {"queued"}


@pytest.mark.asyncio
async def test_public_client_does_not_cache_start_beyond_the_durable_lease() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    service = AdmissionService(store, publisher)
    client = CachedAdmissionClient(DirectAdmissionClient(service), status_ttl=60)

    await asyncio.gather(*(client.start("column-rename") for _ in range(20)))
    await asyncio.gather(*(client.get("column-rename") for _ in range(20)))

    assert len(publisher.calls) == 1
    assert not hasattr(client, "_starts")
    assert len(client._statuses) == 1


@pytest.mark.asyncio
async def test_status_read_never_admits_or_recovers_work() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    service = AdmissionService(store, publisher)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)

    not_started = await service.get("column-rename", now=now)
    await service.start("column-rename", now=now)
    expired = await service.get(
        "column-rename",
        now=now + timedelta(minutes=66),
    )

    assert not_started["status"] == "not_started"
    assert expired["status"] == "queued"
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_result_service_recomputes_terminal_evidence_from_only_the_program() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    admission = AdmissionService(store, publisher)
    receipt = await admission.start("column-rename")
    call = publisher.calls[0]
    task = TaskRequest(**call)
    lease = await ResultService(store).preflight(task)
    assert lease.execution_token is not None
    plan = load_scenario("column-rename").expected_plan.model_copy(
        update={
            "rationale": "untrusted worker prose",
            "evidence": ["untrusted"],
            "confidence": 0.01,
        }
    )

    terminal = await ResultService(store).complete(
        WorkerProposal(
            case_kind="fixture",
            case_id="column-rename",
            event_id=receipt["id"],
            issued_day=call["issued_day"],
            attempt_id=call["attempt_id"],
            execution_token=lease.execution_token,
            program=program_from_legacy_plan(plan),
        )
    )

    assert terminal["status"] == "repaired"
    assert "untrusted worker prose" not in terminal["summary"]
    assert terminal["program"]["rationale"] != "untrusted worker prose"
    assert terminal["program"]["confidence"] == 1
    assert terminal["application"]["state"] == "applied"
    assert terminal["application"]["version"] == 1
    assert terminal["application"]["affected_outputs"] == ["name"]
    assert terminal["application"]["previous_sha256"] != terminal["application"]["applied_sha256"]
    assert terminal["application"]["rollback_ready"] is True
    active = await store.get_active_configuration("column-rename")
    assert active is not None
    assert active["version"] == 1
    assert active["configuration"]["fields"]["name"] == "full_name"
    assert active["previous_configuration"]["fields"]["name"] == "name"
    assert active["source_event_id"] == receipt["id"]
    assert all(not key.startswith("_") for key in terminal)


@pytest.mark.asyncio
async def test_repeated_repair_records_already_active_without_new_version() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    admission = AdmissionService(store, publisher)
    service = ResultService(store)
    plan = load_scenario("column-rename").expected_plan
    terminals = []

    for day in (12, 13):
        now = datetime(2026, 8, day, 15, 0, tzinfo=UTC)
        receipt = await admission.start("column-rename", now=now)
        call = publisher.calls[-1]
        lease = await service.preflight(TaskRequest(**call), now=now)
        assert lease.execution_token is not None
        terminals.append(
            await service.complete(
                WorkerProposal(
                    case_kind="fixture",
                    case_id="column-rename",
                    event_id=receipt["id"],
                    issued_day=call["issued_day"],
                    attempt_id=call["attempt_id"],
                    execution_token=lease.execution_token,
                    program=program_from_legacy_plan(plan),
                ),
                now=now,
            )
        )

    assert terminals[0]["application"]["state"] == "applied"
    assert terminals[1]["application"]["state"] == "already_active"
    assert terminals[1]["application"]["version"] == 1
    active = await store.get_active_configuration("column-rename")
    assert active is not None
    assert active["version"] == 1
    assert active["source_event_id"] == terminals[0]["id"]


@pytest.mark.asyncio
async def test_terminal_receipt_preserves_scheduler_origin_and_source_digest() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    admission = AdmissionService(store, publisher)
    digest = "a" * 64
    receipt = await admission.start(
        "column-rename",
        trigger="cloud-scheduler",
        source_sha256=digest,
    )
    call = publisher.calls[0]
    service = ResultService(store)
    lease = await service.preflight(TaskRequest(**call))
    assert lease.execution_token is not None

    terminal = await service.complete(
        WorkerProposal(
            case_kind="fixture",
            case_id="column-rename",
            event_id=receipt["id"],
            issued_day=call["issued_day"],
            attempt_id=call["attempt_id"],
            execution_token=lease.execution_token,
            program=program_from_legacy_plan(
                load_scenario("column-rename").expected_plan
            ),
        )
    )

    assert terminal["trigger"] == "cloud-scheduler"
    assert terminal["source_sha256"] == digest


@pytest.mark.asyncio
async def test_result_service_rejects_unadmitted_and_external_proposals() -> None:
    store = InMemoryEventStore()
    today = datetime.now(UTC).date()
    demo = load_scenario("column-rename")
    external = load_scenario("italy-compatible-notes")

    with pytest.raises(RuntimeError, match="active execution"):
        await ResultService(store).complete(
            WorkerProposal(
                case_kind="fixture",
                case_id=demo.id,
                event_id=daily_event_id(demo.id, today),
                issued_day=today.isoformat(),
                attempt_id="11111111-1111-4111-8111-111111111111",
                execution_token="22222222-2222-4222-8222-222222222222",
                program=program_from_legacy_plan(demo.expected_plan),
            )
        )
    with pytest.raises(ValueError, match="Unknown incident"):
        await ResultService(store).complete(
            WorkerProposal(
                case_kind="fixture",
                case_id=external.id,
                event_id="not-authorized",
                issued_day=today.isoformat(),
                attempt_id="11111111-1111-4111-8111-111111111111",
                execution_token="22222222-2222-4222-8222-222222222222",
                program=program_from_legacy_plan(external.expected_plan),
            )
        )


@pytest.mark.asyncio
async def test_explicit_start_recovers_an_expired_attempt_with_a_new_task() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    service = AdmissionService(store, publisher)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)

    await service.start("column-rename", now=now)
    await service.start("column-rename", now=now + timedelta(minutes=66))

    assert len(publisher.calls) == 2
    assert publisher.calls[0]["event_id"] == publisher.calls[1]["event_id"]
    assert publisher.calls[0]["attempt_id"] != publisher.calls[1]["attempt_id"]

    with pytest.raises(AdmissionError, match="budget exhausted"):
        await service.start("column-rename", now=now + timedelta(minutes=132))
    assert len(publisher.calls) == 2


@pytest.mark.asyncio
async def test_enqueue_failures_cannot_reset_the_daily_dispatch_budget() -> None:
    publisher = FailingPublisher()
    service = AdmissionService(InMemoryEventStore(), publisher)

    for _ in range(2):
        with pytest.raises(AdmissionError, match="delivery is unavailable"):
            await service.start("column-rename")
    with pytest.raises(AdmissionError, match="budget exhausted"):
        await service.start("column-rename")

    assert publisher.calls == 2


@pytest.mark.asyncio
async def test_stale_execution_cannot_finalize_a_reacquired_attempt() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    service = AdmissionService(store, publisher)
    result_service = ResultService(store)
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    receipt = await service.start("column-rename", now=now)
    first = publisher.calls[0]
    lease = await result_service.preflight(TaskRequest(**first), now=now)
    assert lease.execution_token is not None
    await service.start("column-rename", now=now + timedelta(minutes=66))

    with pytest.raises(RuntimeError, match="active execution"):
        await result_service.complete(
            WorkerProposal(
                case_kind="fixture",
                case_id="column-rename",
                event_id=receipt["id"],
                issued_day=first["issued_day"],
                attempt_id=first["attempt_id"],
                execution_token=lease.execution_token,
                program=program_from_legacy_plan(
                    load_scenario("column-rename").expected_plan
                ),
            ),
            now=now + timedelta(minutes=66),
        )


@pytest.mark.asyncio
async def test_rejected_model_plans_retry_before_bounded_terminal_failure() -> None:
    store = InMemoryEventStore()
    publisher = RecordingPublisher()
    admission = AdmissionService(store, publisher)
    result_service = ResultService(store)
    await admission.start("column-rename")
    task = TaskRequest(**publisher.calls[0])
    rejected = RepairPlan(
        operation="no_change",
        confidence=1,
        evidence=["model claim"],
        rationale="model claim",
    )

    for _ in range(4):
        lease = await result_service.preflight(task)
        assert lease.execution_token is not None
        with pytest.raises(ProposalRejected):
            await result_service.complete(
                WorkerProposal(
                    case_kind=task.case_kind,
                    case_id=task.case_id,
                    event_id=task.event_id,
                    issued_day=task.issued_day,
                    attempt_id=task.attempt_id,
                    execution_token=lease.execution_token,
                    program=program_from_legacy_plan(rejected),
                )
            )

    lease = await result_service.preflight(task)
    assert lease.execution_token is not None
    terminal = await result_service.complete(
        WorkerProposal(
            case_kind=task.case_kind,
            case_id=task.case_id,
            event_id=task.event_id,
            issued_day=task.issued_day,
            attempt_id=task.attempt_id,
            execution_token=lease.execution_token,
            program=program_from_legacy_plan(rejected),
        )
    )

    assert terminal["status"] == "failed"
