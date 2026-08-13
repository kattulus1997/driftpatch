from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.event_identity import daily_event_id, task_id
from app.fast_api_app import create_worker_app
from app.ledger import InMemoryEventStore
from app.result_service import DirectResultPublisher, ResultService


def test_worker_surface_is_minimal_and_rejects_untrusted_delivery(monkeypatch) -> None:
    monkeypatch.setenv("FIRESTORE_ENABLED", "false")
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "driftpatch-worker")
    application = create_worker_app()

    paths = {route.path for route in application.routes}
    assert "/tasks/run" in paths
    assert not any(path.startswith(("/apps", "/a2a", "/run_")) for path in paths)

    with TestClient(application) as client:
        assert client.get("/health").json() == {"status": "ok", "role": "worker"}
        assert client.post("/feedback", json={}).status_code == 404
        assert client.post("/tasks/run", json={}).status_code == 422


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("DRIFTPATCH_LIVE_MODEL_TESTS", "").lower() != "true",
    reason="Set DRIFTPATCH_LIVE_MODEL_TESTS=true to run live model tests",
)
def test_worker_task_runs_the_real_workflow_when_credentials_are_available(
    monkeypatch,
) -> None:
    enterprise_enabled = (
        os.getenv("GOOGLE_GENAI_USE_ENTERPRISE", "").lower() == "true"
    )
    if not os.getenv("GEMINI_API_KEY") and not enterprise_enabled:
        pytest.fail("Live model tests require an API key or enterprise credentials")

    monkeypatch.setenv("FIRESTORE_ENABLED", "false")
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "driftpatch-worker")
    store = InMemoryEventStore()
    application = create_worker_app(
        result_publisher=DirectResultPublisher(ResultService(store))
    )
    issued_day = datetime.now(UTC).date()
    event_id = daily_event_id("column-rename", issued_day)
    claim = __import__("asyncio").run(
        store.claim(
            event_id=event_id,
            scenario_id="column-rename",
            trigger="cloud-tasks",
            now=datetime.now(UTC),
            lease=timedelta(minutes=75),
        )
    )
    assert claim.attempt_id is not None and claim.token is not None
    headers = {
        "X-CloudTasks-QueueName": "driftpatch-worker",
        "X-CloudTasks-TaskName": (
            "projects/test/locations/europe-west1/queues/driftpatch-worker/tasks/"
            f"{task_id(event_id, claim.attempt_id)}"
        ),
    }

    with TestClient(application) as client:
        response = client.post(
            "/tasks/run",
            headers=headers,
            json={
                "scenario_id": "column-rename",
                "event_id": event_id,
                "issued_day": issued_day.isoformat(),
                "attempt_id": claim.attempt_id,
                "attempt_token": claim.token,
            },
        )

    assert response.status_code == 204
    result = __import__("asyncio").run(store.get_terminal(event_id))
    assert result is not None
    assert result["status"] == "repaired"
    assert result["plan"]["operation"] == "update_field_sources"
    assert result["application"]["state"] == "applied"
    assert result["application"]["affected_outputs"] == ["name"]
    assert result["application"]["rollback_ready"] is True
    assert all(check["passed"] for check in result["checks"])
