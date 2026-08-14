from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.event_identity import daily_event_id
from app.fast_api_app import (
    _LocalTaskPublisher,
    create_admission_app,
    create_app,
    create_development_app,
    create_public_app,
    create_result_app,
    create_worker_app,
)
from app.ledger import InMemoryEventStore
from app.schemas import TaskRequest


class RecordingAdmission:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.custom_calls = []

    async def start(self, scenario_id: str) -> dict[str, str]:
        event_id = daily_event_id(scenario_id, date.today())
        self.calls.append({"scenario_id": scenario_id, "event_id": event_id})
        return {"id": event_id, "scenario_id": scenario_id, "status": "queued"}

    async def get(self, scenario_id: str) -> dict[str, str]:
        return {
            "id": daily_event_id(scenario_id, date.today()),
            "scenario_id": scenario_id,
            "status": "queued",
        }

    async def start_custom(self, submission) -> dict[str, str]:
        self.custom_calls.append(submission)
        return {
            "id": "custom_0123456789abcdef0123456789abcdef",
            "status": "queued",
            "status_url": "/api/runs/custom_0123456789abcdef0123456789abcdef",
        }

    async def get_custom(self, run_id: str) -> dict[str, str]:
        return {
            "id": run_id,
            "status": "queued",
            "status_url": f"/api/runs/{run_id}",
        }


@pytest.mark.asyncio
async def test_local_worker_failure_is_logged_instead_of_silently_consumed(
    monkeypatch, caplog
) -> None:
    async def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("visible worker failure")

    monkeypatch.setattr("app.fast_api_app._run_local_incident", fail)
    application = FastAPI()
    application.state.local_tasks = set()
    publisher = _LocalTaskPublisher(application, object())
    request = TaskRequest(
        case_kind="fixture",
        case_id="column-rename",
        event_id="9462c403-6afe-5220-8a07-967191220d3a",
        issued_day=date.today().isoformat(),
        attempt_id="11111111-1111-4111-8111-111111111111",
        attempt_token="22222222-2222-4222-8222-222222222222",
    )

    with caplog.at_level(logging.ERROR, logger="driftpatch.local"):
        await publisher.publish(request)
        while application.state.local_tasks:
            await asyncio.sleep(0)

    assert "Local worker failed for column-rename" in caplog.text
    assert "visible worker failure" in caplog.text


def _routes(application):
    for route in application.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from original_router.routes
        else:
            yield route


def _mutation_paths(application) -> set[str]:
    return {
        route.path
        for route in _routes(application)
        if getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}
    }


def test_public_service_exposes_only_bounded_fixture_and_custom_run_mutations() -> None:
    application = create_public_app(admission=RecordingAdmission())

    assert _mutation_paths(application) == {
        "/api/scenarios/{scenario_id}/run",
        "/api/runs",
    }
    assert not any(
        route.path.startswith(("/apps", "/a2a", "/run", "/feedback", "/agent-identity"))
        for route in _routes(application)
    )


def test_worker_exposes_only_the_private_task_mutation() -> None:
    public_paths = {route.path for route in _routes(create_public_app())}
    worker_paths = {route.path for route in _routes(create_worker_app())}

    assert "/tasks/run" not in public_paths
    assert "/tasks/run" in worker_paths
    assert _mutation_paths(create_worker_app()) == {"/tasks/run"}
    assert not any(path.startswith(("/apps", "/a2a", "/run")) for path in worker_paths)


def test_control_services_expose_disjoint_private_mutations() -> None:
    admission = create_admission_app(store=InMemoryEventStore())
    result = create_result_app(store=InMemoryEventStore())

    assert _mutation_paths(admission) == {
        "/internal/scenarios/{scenario_id}/run",
        "/internal/runs",
        "/internal/watch",
    }
    assert _mutation_paths(result) == {
        "/internal/attempts/preflight",
        "/internal/reconcile",
        "/internal/results",
    }
    assert "/internal/results" not in {route.path for route in _routes(admission)}
    assert "/internal/scenarios/{scenario_id}/run" not in {
        route.path for route in _routes(result)
    }
    assert "/internal/watch" not in {route.path for route in _routes(result)}


def test_default_role_is_public_and_unknown_roles_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("SERVICE_ROLE", raising=False)

    assert _mutation_paths(create_app()) == {
        "/api/scenarios/{scenario_id}/run",
        "/api/runs",
    }

    monkeypatch.setenv("SERVICE_ROLE", "typo")
    try:
        create_app()
    except RuntimeError as exc:
        assert "SERVICE_ROLE" in str(exc)
    else:
        raise AssertionError("an unknown service role must not start")


def test_development_role_keeps_the_same_bounded_http_surface() -> None:
    application = create_development_app()

    assert _mutation_paths(application) == {
        "/api/scenarios/{scenario_id}/run",
        "/api/runs",
    }
    assert not any(
        route.path.startswith(("/apps", "/a2a", "/run"))
        for route in _routes(application)
    )


def test_public_run_publishes_an_allowlisted_event_and_returns_a_receipt() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))

    response = client.post("/api/scenarios/column-rename/run")

    assert response.status_code == 202
    receipt: dict[str, Any] = response.json()
    assert receipt == {
        "id": admission.calls[0]["event_id"],
        "scenario_id": "column-rename",
        "status": "queued",
    }
    assert admission.calls[0]["scenario_id"] == "column-rename"
    assert admission.calls[0]["event_id"] == receipt["id"]


def test_public_run_uses_one_opaque_event_identifier_per_incident_and_utc_day() -> None:
    first = daily_event_id("column-rename", date(2026, 8, 12))
    repeated = daily_event_id("column-rename", date(2026, 8, 12))
    tomorrow = daily_event_id("column-rename", date(2026, 8, 13))
    other_incident = daily_event_id("delimiter-change", date(2026, 8, 12))

    assert UUID(first).version == 5
    assert first == repeated
    assert len({first, tomorrow, other_incident}) == 3


def test_public_repeats_reuse_the_daily_identifier() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))

    first = client.post("/api/scenarios/column-rename/run")
    repeated = client.post("/api/scenarios/column-rename/run")

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json()["id"] == repeated.json()["id"]
    assert {call["event_id"] for call in admission.calls} == {first.json()["id"]}


def test_public_run_rejects_unknown_scenarios_before_publish() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))

    response = client.post("/api/scenarios/not-real/run")

    assert response.status_code == 404
    assert admission.calls == []


def test_public_pending_lookup_is_bounded_to_an_allowlisted_incident() -> None:
    client = TestClient(create_public_app(admission=RecordingAdmission()))

    response = client.get("/api/scenarios/column-rename/run")

    assert response.status_code == 202
    assert response.json() == {
        "id": daily_event_id("column-rename", date.today()),
        "scenario_id": "column-rename",
        "status": "queued",
    }
    assert client.get("/api/runs/9edc876c-d738-424c-88d0-a80278d6c985").status_code == 404
    assert client.get("/api/scenarios/not-real/run").status_code == 404


def _custom_payload() -> dict:
    return {
        "label": "Judge orders",
        "before": {"format": "csv", "content": "id,total\n1,10\n"},
        "after": {"format": "json", "content": '[{"id":1,"total":10}]'},
        "pipeline_json": json.dumps(
            {
                "format": "csv",
                "fields": {"id": "id", "total": "total"},
                "casts": {"id": "integer", "total": "integer"},
            }
        ),
        "contract_json": json.dumps(
            {
                "required": ["id", "total"],
                "types": {"id": "integer", "total": "integer"},
                "unique_key": "id",
            }
        ),
    }


def test_public_custom_run_accepts_all_four_documents_and_returns_status_url() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))

    response = client.post("/api/runs", json=_custom_payload())

    assert response.status_code == 202
    assert response.json() == {
        "id": "custom_0123456789abcdef0123456789abcdef",
        "status": "queued",
        "status_url": "/api/runs/custom_0123456789abcdef0123456789abcdef",
    }
    assert len(admission.custom_calls) == 1
    assert admission.custom_calls[0].before.content == "id,total\n1,10\n"


def test_malformed_custom_request_never_calls_admission() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))

    missing = client.post("/api/runs", json={"label": "broken"})
    invalid_json = client.post(
        "/api/runs",
        content=b'{"label":',
        headers={"Content-Type": "application/json"},
    )

    assert missing.status_code == 422
    assert invalid_json.status_code == 400
    assert admission.custom_calls == []


def test_custom_status_lookup_accepts_only_opaque_custom_ids() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))
    run_id = "custom_0123456789abcdef0123456789abcdef"

    queued = client.get(f"/api/runs/{run_id}")

    assert queued.status_code == 202
    assert queued.json()["id"] == run_id
    assert client.get("/api/runs/../../driftpatch-configurations").status_code == 404
    assert client.get("/api/runs/custom_short").status_code == 404


def test_curated_example_returns_real_editable_source_and_contract_documents() -> None:
    client = TestClient(create_public_app(admission=RecordingAdmission()))

    response = client.get("/api/examples/column-rename")

    assert response.status_code == 200
    payload = response.json()
    assert payload["before"]["format"] == "csv"
    assert "Central Library" in payload["before"]["content"]
    assert json.loads(payload["pipeline_json"])["fields"]["name"] == "name"
    assert json.loads(payload["contract_json"])["unique_key"] == "id"


def test_public_custom_body_limit_rejects_before_admission() -> None:
    admission = RecordingAdmission()
    client = TestClient(create_public_app(admission=admission))

    response = client.post(
        "/api/runs",
        content=b"x" * (5 * 1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert admission.custom_calls == []


def test_public_scenarios_reveal_neither_expected_decisions_nor_static_proof() -> None:
    client = TestClient(create_public_app(admission=RecordingAdmission()))

    response = client.get("/api/scenarios")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"items"}
    assert payload["items"]
    assert all("expected_status" not in item for item in payload["items"])


def test_public_service_serves_both_declared_and_legacy_favicon_paths() -> None:
    client = TestClient(create_public_app(admission=RecordingAdmission()))

    declared = client.get("/favicon.svg")
    legacy = client.get("/favicon.ico")

    assert declared.status_code == legacy.status_code == 200
    assert declared.content == legacy.content
