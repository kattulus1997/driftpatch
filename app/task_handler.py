from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import HTTPException, Request, Response, status
from google.genai import types
from opentelemetry import trace

from .admission import DEMO_SCENARIO_IDS
from .event_identity import daily_event_id, parse_issued_day, task_id
from .execution import ExecutionBinding, bind_execution
from .schemas import TaskRequest

_tracer = trace.get_tracer("driftpatch.worker")


def _canonical_request(payload: TaskRequest, task_name: str, now: datetime) -> None:
    if payload.scenario_id not in DEMO_SCENARIO_IDS:
        raise HTTPException(status_code=400, detail="Unknown incident.")
    try:
        issued_day = parse_issued_day(payload.issued_day, now=now)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    expected_event_id = daily_event_id(payload.scenario_id, issued_day)
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
            "driftpatch.scenario_id": payload.scenario_id,
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

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=json.dumps(
                    {
                        "scenario_id": payload.scenario_id,
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
