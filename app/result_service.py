from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .admission import DEMO_SCENARIO_IDS
from .benchmark import load_scenario
from .event_identity import daily_event_id, parse_issued_day
from .gate import apply_plan_deterministically, validate_plan_deterministically
from .ledger import EventStore
from .repairs import affected_output_fields
from .schemas import AttemptLease, TaskRequest, WorkerProposal

EXECUTION_LEASE = timedelta(minutes=11)


class ProposalRejected(RuntimeError):
    pass


class ResultService:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    @staticmethod
    def _validate_identity(
        *, scenario_id: str, event_id: str, issued_day: str, now: datetime
    ) -> None:
        if scenario_id not in DEMO_SCENARIO_IDS:
            raise ValueError("Unknown incident")
        parsed_day = parse_issued_day(issued_day, now=now)
        if event_id != daily_event_id(scenario_id, parsed_day):
            raise ValueError("Event identity mismatch")

    async def preflight(
        self,
        task: TaskRequest,
        *,
        now: datetime | None = None,
    ) -> AttemptLease:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._validate_identity(
            scenario_id=task.scenario_id,
            event_id=task.event_id,
            issued_day=task.issued_day,
            now=current,
        )
        return await self._store.preflight(
            event_id=task.event_id,
            scenario_id=task.scenario_id,
            attempt_id=task.attempt_id,
            attempt_token=task.attempt_token,
            now=current,
            lease=EXECUTION_LEASE,
        )

    async def complete(
        self,
        proposal: WorkerProposal,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._validate_identity(
            scenario_id=proposal.scenario_id,
            event_id=proposal.event_id,
            issued_day=proposal.issued_day,
            now=current,
        )
        scenario = load_scenario(proposal.scenario_id)
        applied = apply_plan_deterministically(proposal.scenario_id, proposal.plan)
        result = validate_plan_deterministically(applied)
        if result.status == "failed":
            terminal = await self._store.reject_proposal(
                result=result,
                event_id=proposal.event_id,
                scenario_id=proposal.scenario_id,
                attempt_id=proposal.attempt_id,
                execution_token=proposal.execution_token,
                trigger="cloud-tasks",
                now=current,
            )
            if terminal is None:
                raise ProposalRejected("proposal failed the deterministic gate")
            return terminal
        return await self._store.commit_terminal(
            result=result,
            event_id=proposal.event_id,
            scenario_id=proposal.scenario_id,
            attempt_id=proposal.attempt_id,
            execution_token=proposal.execution_token,
            trigger="cloud-tasks",
            now=current,
            base_configuration=scenario.pipeline,
            candidate_configuration=applied.patched_pipeline,
            affected_outputs=tuple(
                sorted(
                    affected_output_fields(
                        scenario.pipeline,
                        result.plan,
                    )
                )
            ),
        )


class DirectResultPublisher:
    def __init__(self, service: ResultService) -> None:
        self._service = service

    async def preflight(self, task: TaskRequest) -> AttemptLease:
        return await self._service.preflight(task)

    async def publish(self, proposal: WorkerProposal) -> dict[str, Any]:
        return await self._service.complete(proposal)
