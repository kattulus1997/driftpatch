from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from fastapi import HTTPException, Request, Response, status
from google.genai import types
from opentelemetry import trace

from .admission import DEMO_SCENARIO_IDS
from .benchmark import load_scenario, scenario_case
from .bundle_store import BundleError
from .case_data import SubmissionRejected, parse_submission
from .event_identity import daily_event_id, parse_issued_day, task_id
from .execution import ExecutionBinding, bind_execution
from .schemas import TaskRequest

_tracer = trace.get_tracer("driftpatch.worker")


def _canonical_request(payload: TaskRequest, task_name: str, now: datetime) -> None:
    try:
        issued_day = parse_issued_day(payload.issued_day, now=now)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.case_kind == "fixture":
        if payload.case_id not in DEMO_SCENARIO_IDS:
            raise HTTPException(status_code=400, detail="Unknown incident.")
        expected_event_id = daily_event_id(payload.case_id, issued_day)
    else:
        if not re.fullmatch(r"custom_[0-9a-f]{32}", payload.case_id):
            raise HTTPException(status_code=400, detail="Unknown custom run.")
        expected_event_id = payload.case_id
    if payload.event_id != expected_event_id:
        raise HTTPException(status_code=400, detail="Event identity mismatch.")
    if task_name.rsplit("/", 1)[-1] != task_id(
        expected_event_id, payload.attempt_id
    ):
        raise HTTPException(status_code=400, detail="Task identity mismatch.")


async def run_task(request: Request, payload: TaskRequest) -> Response:
    with _tracer.start_as_current_span(
        "driftpatch.task.run",
        attributes={
            "driftpatch.case_kind": payload.case_kind,
            "driftpatch.case_id": payload.case_id,
            "driftpatch.event_id": payload.event_id,
            "driftpatch.attempt_id": payload.attempt_id,
        },
    ):
        return await _run_task(request, payload)


async def _run_task(request: Request, payload: TaskRequest) -> Response:
    now = datetime.now(UTC)
    task_name = request.headers.get("X-CloudTasks-TaskName", "")
    queue_name = request.headers.get("X-CloudTasks-QueueName", "")
    expected_queue = request.app.state.task_queue
    if not task_name or queue_name != expected_queue:
        raise HTTPException(status_code=403, detail="Unexpected task queue.")
    _canonical_request(payload, task_name, now)
    lease = await request.app.state.result_publisher.preflight(payload)
    if lease.disposition in {"terminal", "stale"}:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if lease.disposition == "busy":
        raise HTTPException(status_code=409, detail="Execution is already active.")
    if lease.execution_token is None:
        raise HTTPException(status_code=500, detail="Execution lease was not issued.")

    try:
        if payload.case_kind == "fixture":
            case = scenario_case(load_scenario(payload.case_id))
        else:
            if payload.bundle is None:
                raise RuntimeError("custom task has no bundle reference")
            submission = await request.app.state.bundle_store.get(payload.bundle)
            case = parse_submission(submission, case_id=payload.case_id)
    except (BundleError, SubmissionRejected, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Case bundle is unavailable.",
        ) from exc

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=json.dumps(
                    {
                        "case_kind": payload.case_kind,
                        "case_id": payload.case_id,
                        "attributes": {
                            "event_id": payload.event_id,
                            "issued_day": payload.issued_day,
                            "trigger": "cloud-tasks",
                        },
                    },
                    separators=(",", ":"),
                )
            )
        ],
    )
    execution = ExecutionBinding(
        event_id=payload.event_id,
        issued_day=payload.issued_day,
        attempt_id=payload.attempt_id,
        execution_token=lease.execution_token,
        publisher=request.app.state.result_publisher,
        case_kind=payload.case_kind,
        case_id=payload.case_id,
        bundle=payload.bundle,
        case=case,
    )
    with bind_execution(execution):
        async for _ in request.app.state.runner.run_async(
            user_id="cloud-tasks",
            session_id=payload.event_id,
            new_message=message,
        ):
            pass
    if not execution.published:
        raise HTTPException(
            status_code=500,
            detail="Worker completed without publishing a proposal.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
