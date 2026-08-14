from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request

from app.bundle_store import InMemoryBundleStore
from app.event_identity import daily_event_id, task_id
from app.execution import current_execution
from app.fast_api_app import PRIVATE_BODY_LIMIT, create_worker_app
from app.schemas import (
    AttemptLease,
    CustomRunSubmission,
    SourceDocument,
    TaskRequest,
)
from app.task_handler import run_task

TODAY = datetime.now(UTC).date()
EVENT_ID = daily_event_id("column-rename", TODAY)
ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_TOKEN = "22222222-2222-4222-8222-222222222222"
EXECUTION_TOKEN = "33333333-3333-4333-8333-333333333333"
HEADERS = {
    "X-CloudTasks-QueueName": "test-queue",
    "X-CloudTasks-TaskName": f"projects/p/locations/l/queues/test-queue/tasks/{task_id(EVENT_ID, ATTEMPT_ID)}",
}
PAYLOAD = {
    "case_kind": "fixture",
    "case_id": "column-rename",
    "event_id": EVENT_ID,
    "issued_day": TODAY.isoformat(),
    "attempt_id": ATTEMPT_ID,
    "attempt_token": ATTEMPT_TOKEN,
}


class Publisher:
    def __init__(self, dispositions: list[str] | None = None) -> None:
        self.dispositions = dispositions or ["run"]

    async def preflight(self, task):
        del task
        disposition = self.dispositions.pop(0)
        return AttemptLease(
            disposition=disposition,
            execution_token=(EXECUTION_TOKEN if disposition == "run" else None),
        )

    async def publish(self, proposal):
        return proposal.model_dump(mode="json")


class Runner:
    def __init__(self, *, publish: bool, fail: bool = False):
        self.publish = publish
        self.fail = fail
        self.calls = 0
        self.messages: list[str] = []

    async def run_async(self, *, new_message, **kwargs):
        del kwargs
        self.calls += 1
        self.messages.append(new_message.parts[0].text)
        if self.fail:
            raise RuntimeError("transient failure")
        if self.publish:
            execution = current_execution()
            assert execution is not None
            assert execution.case is not None
            execution.published = True
        if False:
            yield None


def _app(
    runner: Runner,
    publisher: Publisher | None = None,
    bundle_store: InMemoryBundleStore | None = None,
) -> FastAPI:
    application = FastAPI()
    application.state.runner = runner
    application.state.result_publisher = publisher or Publisher()
    application.state.task_queue = "test-queue"
    application.state.bundle_store = bundle_store or InMemoryBundleStore()

    @application.post("/tasks/run", status_code=204)
    async def task(request: Request, payload: TaskRequest):
        return await run_task(request, payload)

    return application


@pytest.mark.asyncio
async def test_task_requires_a_published_proposal() -> None:
    runner = Runner(publish=False)
    transport = httpx.ASGITransport(app=_app(runner))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        response = await client.post("/tasks/run", json=PAYLOAD, headers=HEADERS)

    assert response.status_code == 500
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_terminal_retry_is_acknowledged_without_repeating_inference() -> None:
    runner = Runner(publish=True)
    publisher = Publisher(["run", "terminal"])
    transport = httpx.ASGITransport(app=_app(runner, publisher))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        first = await client.post("/tasks/run", json=PAYLOAD, headers=HEADERS)
        second = await client.post("/tasks/run", json=PAYLOAD, headers=HEADERS)

    assert first.status_code == 204
    assert second.status_code == 204
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_active_execution_retries_without_repeating_inference() -> None:
    runner = Runner(publish=True)
    transport = httpx.ASGITransport(app=_app(runner, Publisher(["busy"])))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        response = await client.post("/tasks/run", json=PAYLOAD, headers=HEADERS)

    assert response.status_code == 409
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_task_rejects_queue_event_day_task_and_suite_mismatches() -> None:
    runner = Runner(publish=True)
    transport = httpx.ASGITransport(app=_app(runner))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        wrong_queue = await client.post(
            "/tasks/run",
            json=PAYLOAD,
            headers={**HEADERS, "X-CloudTasks-QueueName": "other"},
        )
        wrong_event = await client.post(
            "/tasks/run",
            json={**PAYLOAD, "event_id": "not-canonical"},
            headers=HEADERS,
        )
        wrong_task = await client.post(
            "/tasks/run",
            json=PAYLOAD,
            headers={**HEADERS, "X-CloudTasks-TaskName": "tasks/run-other"},
        )
        stale = await client.post(
            "/tasks/run",
            json={**PAYLOAD, "issued_day": "2025-01-01"},
            headers=HEADERS,
        )
        external_id = "italy-compatible-notes"
        external_event = daily_event_id(external_id, TODAY)
        external = await client.post(
            "/tasks/run",
                json={
                    "case_kind": "fixture",
                    "case_id": external_id,
                    "event_id": external_event,
                    "issued_day": TODAY.isoformat(),
                    "attempt_id": ATTEMPT_ID,
                    "attempt_token": ATTEMPT_TOKEN,
                },
            headers={
                **HEADERS,
                "X-CloudTasks-TaskName": (
                    "projects/p/locations/l/queues/test-queue/tasks/"
                    f"{task_id(external_event, ATTEMPT_ID)}"
                ),
            },
        )

    assert [
        wrong_queue.status_code,
        wrong_event.status_code,
        wrong_task.status_code,
        stale.status_code,
        external.status_code,
    ] == [403, 400, 400, 400, 400]
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_custom_task_loads_exact_bundle_without_putting_rows_in_session() -> None:
    run_id = "custom_0123456789abcdef0123456789abcdef"
    bundles = InMemoryBundleStore()
    reference = await bundles.put(
        run_id,
        CustomRunSubmission(
            label="Judge source",
            before=SourceDocument(
                format="csv", content="id,name\n1,secret-row-value\n"
            ),
            after=SourceDocument(
                format="csv", content="id,display_name\n1,secret-row-value\n"
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
        ),
    )
    payload = {
        "case_kind": "custom",
        "case_id": run_id,
        "event_id": run_id,
        "issued_day": TODAY.isoformat(),
        "attempt_id": ATTEMPT_ID,
        "attempt_token": ATTEMPT_TOKEN,
        "bundle": reference.model_dump(mode="json"),
    }
    headers = {
        "X-CloudTasks-QueueName": "test-queue",
        "X-CloudTasks-TaskName": (
            "projects/p/locations/l/queues/test-queue/tasks/"
            f"{task_id(run_id, ATTEMPT_ID)}"
        ),
    }
    runner = Runner(publish=True)
    transport = httpx.ASGITransport(app=_app(runner, bundle_store=bundles))

    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        response = await client.post("/tasks/run", json=payload, headers=headers)

    assert response.status_code == 204
    assert runner.calls == 1
    assert json.loads(runner.messages[0]) == {
        "case_kind": "custom",
        "case_id": run_id,
        "attributes": {
            "event_id": run_id,
            "issued_day": TODAY.isoformat(),
            "trigger": "cloud-tasks",
        },
    }
    assert "secret-row-value" not in runner.messages[0]


@pytest.mark.asyncio
async def test_private_worker_rejects_oversized_chunked_body() -> None:
    application = create_worker_app(result_publisher=Publisher())
    transport = httpx.ASGITransport(app=application)

    async def body():
        yield b"{" + b"x" * PRIVATE_BODY_LIMIT + b"}"

    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        response = await client.post(
            "/tasks/run",
            content=body(),
            headers={**HEADERS, "Content-Type": "application/json"},
        )

    assert response.status_code == 413
