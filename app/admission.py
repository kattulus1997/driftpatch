from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from functools import cache
from time import monotonic
from typing import Any, Protocol

from .benchmark import load_scenarios
from .event_delivery import EventDeliveryError, EventPublisher
from .event_identity import daily_event_id
from .ledger import ClaimDisposition, EventStore
from .schemas import RunReceipt
from .service_client import ServiceRequestError, request_json

DEMO_SCENARIO_IDS = frozenset(scenario.id for scenario in load_scenarios())
ADMISSION_LEASE = timedelta(minutes=65)


class AdmissionError(RuntimeError):
    pass


class AdmissionClient(Protocol):
    async def start(self, scenario_id: str) -> dict[str, Any]: ...

    async def get(self, scenario_id: str) -> dict[str, Any]: ...


class AdmissionService:
    def __init__(self, store: EventStore, publisher: EventPublisher) -> None:
        self._store = store
        self._publisher = publisher

    @staticmethod
    def require_scenario(scenario_id: str) -> None:
        if scenario_id not in DEMO_SCENARIO_IDS:
            raise ValueError("Unknown incident")

    async def start(
        self,
        scenario_id: str,
        *,
        now: datetime | None = None,
        trigger: str = "public-request",
        source_sha256: str | None = None,
    ) -> dict[str, Any]:
        self.require_scenario(scenario_id)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        issued_day = current.date()
        event_id = daily_event_id(scenario_id, issued_day)
        claim = await self._store.claim(
            event_id=event_id,
            scenario_id=scenario_id,
            trigger=trigger,
            now=current,
            lease=ADMISSION_LEASE,
            source_sha256=source_sha256,
        )
        if claim.disposition is ClaimDisposition.EXHAUSTED:
            raise AdmissionError("daily execution budget exhausted")
        if claim.disposition is ClaimDisposition.ACQUIRED:
            if claim.attempt_id is None or claim.token is None:
                raise AdmissionError("admission claim failed")
            try:
                await self._publisher.publish(
                    scenario_id=scenario_id,
                    event_id=event_id,
                    issued_day=issued_day.isoformat(),
                    attempt_id=claim.attempt_id,
                    attempt_token=claim.token,
                )
            except EventDeliveryError as exc:
                await self._store.release(event_id=event_id, claim_token=claim.token)
                raise AdmissionError("event delivery is unavailable") from exc
        return RunReceipt(
            id=event_id,
            scenario_id=scenario_id,
            status="queued",
        ).model_dump(mode="json")

    async def get(
        self, scenario_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        self.require_scenario(scenario_id)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        event_id = daily_event_id(scenario_id, current.date())
        record = await self._store.get_record(event_id)
        if record is None:
            return {
                "id": event_id,
                "scenario_id": scenario_id,
                "status": "not_started",
            }
        if record.get("status") in {"unchanged", "repaired", "escalated", "failed"}:
            return record
        return RunReceipt(
            id=event_id,
            scenario_id=scenario_id,
            status="queued",
        ).model_dump(mode="json")


class DirectAdmissionClient:
    def __init__(self, service: AdmissionService) -> None:
        self._service = service

    async def start(self, scenario_id: str) -> dict[str, Any]:
        return await self._service.start(scenario_id)

    async def get(self, scenario_id: str) -> dict[str, Any]:
        return await self._service.get(scenario_id)


class HttpAdmissionClient:
    def __init__(self, service_url: str) -> None:
        self._service_url = service_url.rstrip("/")

    async def start(self, scenario_id: str) -> dict[str, Any]:
        try:
            return await request_json(
                "POST",
                f"{self._service_url}/internal/scenarios/{scenario_id}/run",
                audience=self._service_url,
            )
        except ServiceRequestError as exc:
            raise AdmissionError("admission service is unavailable") from exc

    async def get(self, scenario_id: str) -> dict[str, Any]:
        try:
            return await request_json(
                "GET",
                f"{self._service_url}/internal/scenarios/{scenario_id}/run",
                audience=self._service_url,
            )
        except ServiceRequestError as exc:
            raise AdmissionError("admission service is unavailable") from exc


class CachedAdmissionClient:
    def __init__(self, delegate: AdmissionClient, *, status_ttl: float = 2.0) -> None:
        self._delegate = delegate
        self._status_ttl = status_ttl
        self._statuses: dict[tuple[str, date], tuple[float, dict[str, Any]]] = {}
        self._locks: dict[tuple[str, date], asyncio.Lock] = {}

    @staticmethod
    def _key(scenario_id: str) -> tuple[str, date]:
        return scenario_id, datetime.now(UTC).date()

    def _lock(self, key: tuple[str, date]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def start(self, scenario_id: str) -> dict[str, Any]:
        key = self._key(scenario_id)
        async with self._lock(key):
            return (await self._delegate.start(scenario_id)).copy()

    async def get(self, scenario_id: str) -> dict[str, Any]:
        key = self._key(scenario_id)
        async with self._lock(key):
            cached = self._statuses.get(key)
            now = monotonic()
            if cached is None or now - cached[0] >= self._status_ttl:
                self._statuses[key] = (
                    now,
                    await self._delegate.get(scenario_id),
                )
            return self._statuses[key][1].copy()


class UnconfiguredAdmissionClient:
    async def start(self, scenario_id: str) -> dict[str, Any]:
        del scenario_id
        raise AdmissionError("admission service is not configured")

    async def get(self, scenario_id: str) -> dict[str, Any]:
        del scenario_id
        raise AdmissionError("admission service is not configured")


@cache
def configured_admission_client() -> AdmissionClient:
    service_url = os.getenv("ADMISSION_URL", "").strip()
    if not service_url:
        return UnconfiguredAdmissionClient()
    return CachedAdmissionClient(HttpAdmissionClient(service_url))
