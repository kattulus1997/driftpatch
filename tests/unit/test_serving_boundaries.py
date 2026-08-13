from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from app.event_identity import daily_event_id
from app.fast_api_app import (
    create_admission_app,
    create_app,
    create_development_app,
    create_public_app,
    create_result_app,
    create_worker_app,
)
from app.ledger import InMemoryEventStore


class RecordingAdmission:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

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


def test_public_service_exposes_only_the_bounded_run_mutation() -> None:
    application = create_public_app(admission=RecordingAdmission())

    assert _mutation_paths(application) == {"/api/scenarios/{scenario_id}/run"}
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
        "/internal/watch",
    }
    assert _mutation_paths(result) == {
        "/internal/attempts/preflight",
        "/internal/results",
    }
    assert "/internal/results" not in {route.path for route in _routes(admission)}
    assert "/internal/scenarios/{scenario_id}/run" not in {
        route.path for route in _routes(result)
    }
    assert "/internal/watch" not in {route.path for route in _routes(result)}


def test_default_role_is_public_and_unknown_roles_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("SERVICE_ROLE", raising=False)

    assert _mutation_paths(create_app()) == {"/api/scenarios/{scenario_id}/run"}

    monkeypatch.setenv("SERVICE_ROLE", "typo")
    try:
        create_app()
    except RuntimeError as exc:
        assert "SERVICE_ROLE" in str(exc)
    else:
        raise AssertionError("an unknown service role must not start")


def test_development_role_keeps_the_same_bounded_http_surface() -> None:
    application = create_development_app()

    assert _mutation_paths(application) == {"/api/scenarios/{scenario_id}/run"}
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
