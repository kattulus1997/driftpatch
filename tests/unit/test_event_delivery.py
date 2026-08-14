from __future__ import annotations

import json
from datetime import date

import pytest
from google.api_core.exceptions import AlreadyExists

from app.event_delivery import CloudTasksEventPublisher, EventDeliveryError
from app.schemas import StoredBundle, TaskRequest

ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_TOKEN = "22222222-2222-4222-8222-222222222222"


def _fixture_task() -> TaskRequest:
    return TaskRequest(
        case_kind="fixture",
        case_id="column-rename",
        event_id="9462c403-6afe-5220-8a07-967191220d3a",
        issued_day=date.today().isoformat(),
        attempt_id=ATTEMPT_ID,
        attempt_token=ATTEMPT_TOKEN,
    )


class Client:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


def _publisher(client: Client) -> CloudTasksEventPublisher:
    publisher = object.__new__(CloudTasksEventPublisher)
    publisher._client = client
    publisher._parent = client.queue_path("project", "europe-west1", "queue")
    publisher._worker_url = "https://worker.example"
    publisher._invoker_service_account = "invoker@example.iam.gserviceaccount.com"
    return publisher


@pytest.mark.asyncio
async def test_named_task_is_idempotent_before_worker_execution() -> None:
    client = Client(error=AlreadyExists("duplicate"))
    publisher = _publisher(client)

    await publisher.publish(_fixture_task())

    task = client.calls[0]["task"]
    assert task.name.endswith(
        "/tasks/run-9462c403-6afe-5220-8a07-967191220d3a-" + ATTEMPT_ID
    )
    assert task.http_request.url == "https://worker.example/tasks/run"
    assert task.http_request.oidc_token.service_account_email.endswith(
        "gserviceaccount.com"
    )
    body = json.loads(task.http_request.body)
    assert body["attempt_id"] == ATTEMPT_ID
    assert body["attempt_token"] == ATTEMPT_TOKEN
    assert body["case_kind"] == "fixture"
    assert body["case_id"] == "column-rename"
    assert "scenario_id" not in body


@pytest.mark.asyncio
async def test_custom_queue_payload_contains_only_identity_and_bundle_reference() -> None:
    client = Client(error=AlreadyExists("duplicate"))
    publisher = _publisher(client)
    run_id = "custom_0123456789abcdef0123456789abcdef"
    task = TaskRequest(
        case_kind="custom",
        case_id=run_id,
        event_id=run_id,
        issued_day=date.today().isoformat(),
        attempt_id=ATTEMPT_ID,
        attempt_token=ATTEMPT_TOKEN,
        bundle=StoredBundle(
            object_name=f"custom/{run_id}.json",
            generation=9,
            sha256="b" * 64,
            size_bytes=456,
        ),
    )

    await publisher.publish(task)

    body = json.loads(client.calls[0]["task"].http_request.body)
    assert body == task.model_dump(mode="json")
    assert "content" not in json.dumps(body)


@pytest.mark.asyncio
async def test_concurrent_replay_has_one_canonical_queue_identity() -> None:
    client = Client(error=AlreadyExists("duplicate"))
    publisher = _publisher(client)
    event_id = "9462c403-6afe-5220-8a07-967191220d3a"

    await __import__("asyncio").gather(
        *(
            publisher.publish(_fixture_task())
            for _ in range(20)
        )
    )

    assert len(client.calls) == 20
    assert {call["task"].name for call in client.calls} == {
        f"projects/project/locations/europe-west1/queues/queue/tasks/run-{event_id}-{ATTEMPT_ID}"
    }


@pytest.mark.asyncio
async def test_queue_failure_is_redacted_at_the_public_boundary(caplog) -> None:
    publisher = _publisher(Client(error=RuntimeError("private detail")))

    with pytest.raises(EventDeliveryError, match="Cloud Tasks enqueue failed"):
        await publisher.publish(_fixture_task())

    assert "Cloud Tasks enqueue failed: RuntimeError" in caplog.text
    assert "private detail" not in caplog.text
