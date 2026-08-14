from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .admission import DEMO_SCENARIO_IDS
from .benchmark import load_scenario, scenario_case
from .bundle_store import BundleStore, configured_bundle_store
from .case_data import RepairCase, inspect_case, parse_submission
from .event_identity import daily_event_id, parse_issued_day
from .ledger import EventStore
from .repairs import affected_output_fields, build_candidate_catalogue
from .schemas import (
    AttemptLease,
    CheckResult,
    RepairProgram,
    TaskRequest,
    ValidationResult,
    WorkerProposal,
)
from .synthesis import verify_authoritative_program

EXECUTION_LEASE = timedelta(minutes=11)


class ProposalRejected(RuntimeError):
    pass


class ResultService:
    def __init__(
        self, store: EventStore, bundles: BundleStore | None = None
    ) -> None:
        self._store = store
        self._bundles = bundles or configured_bundle_store()

    @staticmethod
    def _validate_identity(
        *,
        case_kind: str,
        case_id: str,
        event_id: str,
        issued_day: str,
        now: datetime,
    ) -> None:
        parsed_day = parse_issued_day(issued_day, now=now)
        if case_kind == "fixture":
            if case_id not in DEMO_SCENARIO_IDS:
                raise ValueError("Unknown incident")
            expected_event_id = daily_event_id(case_id, parsed_day)
        elif case_kind == "custom" and re.fullmatch(
            r"custom_[0-9a-f]{32}", case_id
        ):
            expected_event_id = case_id
        else:
            raise ValueError("Unknown incident")
        if event_id != expected_event_id:
            raise ValueError("Event identity mismatch")

    async def _bound_custom_bundle(self, task: TaskRequest | WorkerProposal):
        expected = await self._store.get_custom_bundle(task.case_id)
        if expected is None or expected != task.bundle:
            raise ValueError("bundle reference mismatch")
        return expected

    async def _load_case(
        self, task: TaskRequest | WorkerProposal
    ) -> RepairCase:
        if task.case_kind == "fixture":
            return scenario_case(load_scenario(task.case_id))
        reference = await self._bound_custom_bundle(task)
        submission = await self._bundles.get(reference)
        return parse_submission(submission, case_id=task.case_id)

    async def preflight(
        self,
        task: TaskRequest,
        *,
        now: datetime | None = None,
    ) -> AttemptLease:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._validate_identity(
            case_kind=task.case_kind,
            case_id=task.case_id,
            event_id=task.event_id,
            issued_day=task.issued_day,
            now=current,
        )
        if await self._store.get_terminal(task.event_id) is not None:
            return AttemptLease(disposition="terminal")
        if task.case_kind == "custom":
            await self._bound_custom_bundle(task)
        return await self._store.preflight(
            event_id=task.event_id,
            scenario_id=task.case_id,
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
        require_fresh_lease = now is None
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._validate_identity(
            case_kind=proposal.case_kind,
            case_id=proposal.case_id,
            event_id=proposal.event_id,
            issued_day=proposal.issued_day,
            now=current,
        )
        existing = await self._store.get_terminal(proposal.event_id)
        if existing is not None:
            if proposal.case_kind == "custom" and proposal.bundle is not None:
                await self._bundles.delete(proposal.bundle)
            return existing

        case = await self._load_case(proposal)
        catalogue = build_candidate_catalogue(case, inspect_case(case))
        result = verify_authoritative_program(case, proposal.program, catalogue)
        if result.status == "failed":
            terminal = await self._store.reject_proposal(
                result=result,
                event_id=proposal.event_id,
                scenario_id=proposal.case_id,
                attempt_id=proposal.attempt_id,
                execution_token=proposal.execution_token,
                trigger="cloud-tasks",
                now=current,
                require_fresh_lease=require_fresh_lease,
            )
            if terminal is None:
                raise ProposalRejected("proposal failed the deterministic gate")
            if proposal.case_kind == "custom" and proposal.bundle is not None:
                await self._bundles.delete(proposal.bundle)
            return terminal
        terminal = await self._store.commit_terminal(
            result=result,
            event_id=proposal.event_id,
            scenario_id=proposal.case_id,
            attempt_id=proposal.attempt_id,
            execution_token=proposal.execution_token,
            trigger="cloud-tasks",
            now=current,
            base_configuration=case.pipeline,
            candidate_configuration=result.patched_pipeline,
            affected_outputs=tuple(
                sorted(
                    affected_output_fields(
                        case.pipeline,
                        result.program,
                    )
                )
            ),
            require_fresh_lease=require_fresh_lease,
        )
        if proposal.case_kind == "custom" and proposal.bundle is not None:
            await self._bundles.delete(proposal.bundle)
        return terminal

    @staticmethod
    def _stale_result(run_id: str, reason: str) -> ValidationResult:
        program = RepairProgram(
            decision="escalate",
            steps=[],
            confidence=1,
            evidence=[reason.replace("_", " ")],
            rationale=reason,
        )
        return ValidationResult(
            scenario_id=run_id,
            status="escalated",
            program=program,
            checks=[
                CheckResult(
                    name="safe_escalation",
                    passed=True,
                    detail="no configuration was applied after execution expiry",
                )
            ],
            transformed_rows=0,
            evidence_complete=True,
            summary=f"{run_id}: escalated after bounded execution expiry.",
        )

    async def reconcile_stale(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        terminals: list[dict[str, Any]] = []
        for run_id, bundle, reason in await self._store.list_stale_custom(
            now=current, limit=limit
        ):
            terminal = await self._store.terminalize_stale_custom(
                run_id=run_id,
                reason=reason,
                result=self._stale_result(run_id, reason),
                now=current,
            )
            if terminal is None:
                continue
            await self._bundles.delete(bundle)
            terminals.append(terminal)
        return terminals


class DirectResultPublisher:
    def __init__(self, service: ResultService) -> None:
        self._service = service

    async def preflight(self, task: TaskRequest) -> AttemptLease:
        return await self._service.preflight(task)

    async def publish(self, proposal: WorkerProposal) -> dict[str, Any]:
        return await self._service.complete(proposal)
