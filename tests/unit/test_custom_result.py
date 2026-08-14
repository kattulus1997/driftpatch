from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.admission import ADMISSION_LEASE, AdmissionService
from app.bundle_store import BundleMissing, InMemoryBundleStore
from app.case_data import inspect_case, parse_submission
from app.ledger import InMemoryEventStore
from app.repairs import build_candidate_catalogue
from app.result_service import ResultService
from app.schemas import (
    CustomRunSubmission,
    SourceDocument,
    TaskRequest,
    WorkerProposal,
)
from app.synthesis import search_catalogue

NOW = datetime(2026, 8, 13, 18, 0, tzinfo=UTC)


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[TaskRequest] = []

    async def publish(self, task: TaskRequest) -> None:
        self.calls.append(task)


class AdvancingBundles(InMemoryBundleStore):
    def __init__(self, clock: list[datetime]) -> None:
        super().__init__()
        self._clock = clock

    async def get(self, reference):
        self._clock[0] += timedelta(minutes=12)
        return await super().get(reference)


def _submission() -> CustomRunSubmission:
    return CustomRunSubmission(
        label="Uploaded chain",
        before=SourceDocument(
            format="csv", content="id,name\n1,private-value\n"
        ),
        after=SourceDocument(
            format="csv", content="id,display_name\n1,private-value\n"
        ),
        pipeline_json=json.dumps(
            {
                "format": "csv",
                "fields": {"id": "id", "name": "name"},
                "casts": {"id": "integer"},
            }
        ),
        contract_json=json.dumps(
            {
                "required": ["id", "name"],
                "types": {"id": "integer", "name": "string"},
                "unique_key": "id",
                "source_aliases": {"name": ["display_name"]},
                "preserve_values": ["name"],
            }
        ),
    )


@pytest.mark.asyncio
async def test_custom_result_reloads_reverifies_commits_and_deletes_bundle() -> None:
    store = InMemoryEventStore()
    bundles = InMemoryBundleStore()
    publisher = RecordingPublisher()
    await AdmissionService(store, publisher, bundles).start_custom(
        _submission(), now=NOW
    )
    task = publisher.calls[0]
    service = ResultService(store, bundles)
    lease = await service.preflight(task, now=NOW)
    assert lease.execution_token is not None
    case = parse_submission(_submission(), case_id=task.case_id)
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    program = search_catalogue(case, catalogue).model_copy(
        update={"rationale": "untrusted worker prose", "confidence": 0.01}
    )
    proposal = WorkerProposal(
        case_kind="custom",
        case_id=task.case_id,
        event_id=task.event_id,
        issued_day=task.issued_day,
        attempt_id=task.attempt_id,
        execution_token=lease.execution_token,
        bundle=task.bundle,
        program=program,
    )

    terminal = await service.complete(proposal, now=NOW)

    assert terminal["status"] == "repaired"
    assert terminal["program"]["rationale"] == "verified_repair"
    assert terminal["program"]["confidence"] == 1
    assert terminal["patched_pipeline"]["fields"]["name"] == "display_name"
    assert terminal["patched_pipeline_hash"]
    assert terminal["application"]["state"] == "applied"
    assert terminal["application"]["rollback_ready"] is True
    with pytest.raises(BundleMissing):
        await bundles.get(task.bundle)
    assert await service.complete(proposal, now=NOW) == terminal


@pytest.mark.asyncio
async def test_expired_execution_token_cannot_commit_configuration_or_terminal() -> None:
    store = InMemoryEventStore()
    bundles = InMemoryBundleStore()
    publisher = RecordingPublisher()
    await AdmissionService(store, publisher, bundles).start_custom(
        _submission(), now=NOW
    )
    task = publisher.calls[0]
    service = ResultService(store, bundles)
    lease = await service.preflight(task, now=NOW)
    assert lease.execution_token is not None and task.bundle is not None
    case = parse_submission(_submission(), case_id=task.case_id)
    proposal = WorkerProposal(
        case_kind="custom",
        case_id=task.case_id,
        event_id=task.event_id,
        issued_day=task.issued_day,
        attempt_id=task.attempt_id,
        execution_token=lease.execution_token,
        bundle=task.bundle,
        program=search_catalogue(
            case, build_candidate_catalogue(case, inspect_case(case))
        ),
    )

    with pytest.raises(RuntimeError, match="active execution"):
        await service.complete(proposal, now=NOW + timedelta(minutes=12))

    assert await store.get_terminal(task.event_id) is None
    assert await store.get_active_configuration(task.case_id) is None
    assert await bundles.get(task.bundle) == _submission()


@pytest.mark.asyncio
async def test_lease_expiring_during_bundle_verification_cannot_commit() -> None:
    started = datetime.now(UTC)
    clock = [started]
    store = InMemoryEventStore(clock=lambda: clock[0])
    bundles = AdvancingBundles(clock)
    publisher = RecordingPublisher()
    await AdmissionService(store, publisher, bundles).start_custom(
        _submission(), now=started
    )
    task = publisher.calls[0]
    service = ResultService(store, bundles)
    lease = await service.preflight(task, now=started)
    assert lease.execution_token is not None and task.bundle is not None
    case = parse_submission(_submission(), case_id=task.case_id)
    proposal = WorkerProposal(
        case_kind="custom",
        case_id=task.case_id,
        event_id=task.event_id,
        issued_day=task.issued_day,
        attempt_id=task.attempt_id,
        execution_token=lease.execution_token,
        bundle=task.bundle,
        program=search_catalogue(
            case, build_candidate_catalogue(case, inspect_case(case))
        ),
    )

    with pytest.raises(RuntimeError, match="active execution"):
        await service.complete(proposal)

    assert await store.get_terminal(task.event_id) is None
    assert await store.get_active_configuration(task.case_id) is None


@pytest.mark.asyncio
async def test_custom_preflight_rejects_a_reference_not_bound_in_private_ledger() -> None:
    store = InMemoryEventStore()
    bundles = InMemoryBundleStore()
    publisher = RecordingPublisher()
    await AdmissionService(store, publisher, bundles).start_custom(
        _submission(), now=NOW
    )
    task = publisher.calls[0]
    assert task.bundle is not None
    forged = task.model_copy(
        update={
            "bundle": task.bundle.model_copy(update={"sha256": "f" * 64})
        }
    )

    with pytest.raises(ValueError, match="bundle reference mismatch"):
        await ResultService(store, bundles).preflight(forged, now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_execution", "reason"),
    [(False, "delivery_exhausted"), (True, "execution_exhausted")],
)
async def test_reconciliation_terminalizes_stale_custom_runs_once(
    start_execution: bool, reason: str
) -> None:
    store = InMemoryEventStore()
    bundles = InMemoryBundleStore()
    run_id = (
        "custom_11111111111111111111111111111111"
        if start_execution
        else "custom_22222222222222222222222222222222"
    )
    claim = await store.claim_custom(
        candidate_run_id=run_id,
        digest=("1" if start_execution else "2") * 64,
        uses_inference=True,
        now=NOW,
        lease=ADMISSION_LEASE,
        limit=24,
    )
    assert claim.token is not None and claim.attempt_id is not None
    reference = await bundles.put(run_id, _submission())
    assert await store.attach_custom_bundle(
        run_id=run_id, claim_token=claim.token, bundle=reference
    )
    if start_execution:
        assert await store.mark_custom_dispatched(
            run_id=run_id, claim_token=claim.token
        )
        lease = await ResultService(store, bundles).preflight(
            TaskRequest(
                case_kind="custom",
                case_id=run_id,
                event_id=run_id,
                issued_day=NOW.date().isoformat(),
                attempt_id=claim.attempt_id,
                attempt_token=claim.token,
                bundle=reference,
            ),
            now=NOW,
        )
        assert lease.execution_token is not None
        later = NOW + timedelta(minutes=12)
    else:
        later = NOW + timedelta(minutes=66)

    service = ResultService(store, bundles)
    first = await service.reconcile_stale(now=later)
    second = await service.reconcile_stale(now=later)

    assert len(first) == 1
    assert first[0]["status"] == "escalated"
    assert first[0]["program"]["rationale"] == reason
    assert second == []
    with pytest.raises(BundleMissing):
        await bundles.get(reference)
