from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from functools import cache
from time import monotonic
from typing import Any, Protocol

from .benchmark import load_scenarios
from .bundle_store import (
    BundleError,
    BundleStore,
    configured_bundle_store,
)
from .case_data import (
    RepairCase,
    SubmissionRejected,
    parse_submission,
    run_case_contracts,
)
from .event_delivery import EventDeliveryError, EventPublisher
from .event_identity import daily_event_id
from .ledger import ClaimDisposition, EventStore
from .schemas import CustomRunReceipt, CustomRunSubmission, RunReceipt, TaskRequest
from .service_client import ServiceRequestError, request_json

DEMO_SCENARIO_IDS = frozenset(scenario.id for scenario in load_scenarios())
ADMISSION_LEASE = timedelta(minutes=65)


class AdmissionError(RuntimeError):
    pass


class AdmissionRejected(AdmissionError):
    def __init__(self, status_code: int, code: str, detail: str):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class AdmissionClient(Protocol):
    async def start(self, scenario_id: str) -> dict[str, Any]: ...

    async def get(self, scenario_id: str) -> dict[str, Any]: ...

    async def start_custom(
        self, submission: CustomRunSubmission
    ) -> dict[str, Any]: ...

    async def get_custom(self, run_id: str) -> dict[str, Any]: ...


class AdmissionService:
    def __init__(
        self,
        store: EventStore,
        publisher: EventPublisher,
        bundles: BundleStore | None = None,
        *,
        daily_custom_limit: int = 24,
        daily_total_limit: int = 48,
    ) -> None:
        if daily_custom_limit < 1:
            raise ValueError("daily custom limit must be positive")
        if daily_total_limit < 1:
            raise ValueError("daily total limit must be positive")
        self._store = store
        self._publisher = publisher
        self._bundles = bundles or configured_bundle_store()
        self._daily_custom_limit = daily_custom_limit
        self._daily_total_limit = daily_total_limit

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
                    TaskRequest(
                        case_kind="fixture",
                        case_id=scenario_id,
                        event_id=event_id,
                        issued_day=issued_day.isoformat(),
                        attempt_id=claim.attempt_id,
                        attempt_token=claim.token,
                    )
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

    @staticmethod
    def _custom_digest(
        submission: CustomRunSubmission, case: RepairCase
    ) -> str:
        payload = json.dumps(
            {
                "before": submission.before.model_dump(mode="json"),
                "after": submission.after.model_dump(mode="json"),
                "pipeline": case.pipeline.model_dump(mode="json"),
                "contract": case.contract.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _submission_rejection(exc: SubmissionRejected) -> AdmissionRejected:
        if exc.code in {"body_too_large", "source_too_large", "cell_too_large"}:
            status_code = 413
        elif exc.code in {"invalid_baseline", "pipeline_invalid", "contract_invalid"}:
            status_code = 422
        else:
            status_code = 400
        return AdmissionRejected(status_code, exc.code, exc.detail)

    @staticmethod
    def _custom_receipt(run_id: str) -> dict[str, Any]:
        return CustomRunReceipt(
            id=run_id,
            status="queued",
            status_url=f"/api/runs/{run_id}",
        ).model_dump(mode="json")

    async def start_custom(
        self,
        submission: CustomRunSubmission,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            case = parse_submission(submission)
            _, current_checks = run_case_contracts(case, case.pipeline)
            digest = self._custom_digest(submission, case)
        except SubmissionRejected as exc:
            raise self._submission_rejection(exc) from exc
        uses_inference = not (
            bool(current_checks) and all(check.passed for check in current_checks)
        )
        candidate_run_id = f"custom_{secrets.token_hex(16)}"
        try:
            claim = await self._store.claim_custom(
                candidate_run_id=candidate_run_id,
                digest=digest,
                uses_inference=uses_inference,
                now=current,
                lease=ADMISSION_LEASE,
                limit=self._daily_custom_limit,
                total_limit=self._daily_total_limit,
            )
        except Exception as exc:
            raise AdmissionRejected(
                503, "admission_unavailable", "custom admission is unavailable"
            ) from exc
        if claim.disposition == "existing":
            return self._custom_receipt(claim.run_id)
        if claim.disposition == "exhausted":
            if claim.exhausted_budget == "total":
                raise AdmissionRejected(
                    429,
                    "daily_total_limit",
                    "daily custom-run admission limit reached",
                )
            raise AdmissionRejected(
                429,
                "daily_limit",
                "daily inference-bearing custom-run limit reached",
            )
        if claim.attempt_id is None or claim.token is None:
            raise AdmissionRejected(503, "claim_failed", "custom admission failed")

        bundle = None
        try:
            bundle = await self._bundles.put(claim.run_id, submission)
            attached = await self._store.attach_custom_bundle(
                run_id=claim.run_id,
                claim_token=claim.token,
                bundle=bundle,
            )
            if not attached:
                raise BundleError("custom bundle claim is no longer active")
            await self._publisher.publish(
                TaskRequest(
                    case_kind="custom",
                    case_id=claim.run_id,
                    event_id=claim.run_id,
                    issued_day=current.date().isoformat(),
                    attempt_id=claim.attempt_id,
                    attempt_token=claim.token,
                    bundle=bundle,
                )
            )
        except (BundleError, EventDeliveryError, TypeError) as exc:
            if bundle is not None:
                try:
                    await self._bundles.delete(bundle)
                except BundleError:
                    pass
            await self._store.release_custom(
                run_id=claim.run_id, claim_token=claim.token
            )
            raise AdmissionRejected(
                503, "admission_unavailable", "custom admission is unavailable"
            ) from exc
        dispatched = await self._store.mark_custom_dispatched(
            run_id=claim.run_id, claim_token=claim.token
        )
        if not dispatched:
            raise AdmissionRejected(
                503,
                "dispatch_state_unavailable",
                "custom run was queued but its state could not be confirmed",
            )
        return self._custom_receipt(claim.run_id)

    async def get_custom(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"custom_[0-9a-f]{32}", run_id):
            raise ValueError("Unknown custom run")
        record = await self._store.get_record(run_id)
        if record is None or record.get("scenario_id") != run_id:
            raise ValueError("Unknown custom run")
        if record.get("status") not in {
            "unchanged",
            "repaired",
            "escalated",
            "failed",
        }:
            return self._custom_receipt(run_id)
        return {
            key: value
            for key, value in record.items()
            if key not in {"scenario_id", "source_sha256"}
        }


class DirectAdmissionClient:
    def __init__(self, service: AdmissionService) -> None:
        self._service = service

    async def start(self, scenario_id: str) -> dict[str, Any]:
        return await self._service.start(scenario_id)

    async def get(self, scenario_id: str) -> dict[str, Any]:
        return await self._service.get(scenario_id)

    async def start_custom(
        self, submission: CustomRunSubmission
    ) -> dict[str, Any]:
        return await self._service.start_custom(submission)

    async def get_custom(self, run_id: str) -> dict[str, Any]:
        return await self._service.get_custom(run_id)


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

    async def start_custom(
        self, submission: CustomRunSubmission
    ) -> dict[str, Any]:
        try:
            return await request_json(
                "POST",
                f"{self._service_url}/internal/runs",
                audience=self._service_url,
                payload=submission.model_dump(mode="json"),
            )
        except ServiceRequestError as exc:
            if exc.status_code in {400, 413, 422, 429}:
                raise AdmissionRejected(
                    exc.status_code, exc.code or "request_rejected", exc.detail
                ) from exc
            raise AdmissionError("admission service is unavailable") from exc

    async def get_custom(self, run_id: str) -> dict[str, Any]:
        try:
            return await request_json(
                "GET",
                f"{self._service_url}/internal/runs/{run_id}",
                audience=self._service_url,
            )
        except ServiceRequestError as exc:
            if exc.status_code == 404:
                raise ValueError("Unknown custom run") from exc
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

    async def start_custom(
        self, submission: CustomRunSubmission
    ) -> dict[str, Any]:
        return (await self._delegate.start_custom(submission)).copy()

    async def get_custom(self, run_id: str) -> dict[str, Any]:
        return (await self._delegate.get_custom(run_id)).copy()


class UnconfiguredAdmissionClient:
    async def start(self, scenario_id: str) -> dict[str, Any]:
        del scenario_id
        raise AdmissionError("admission service is not configured")

    async def get(self, scenario_id: str) -> dict[str, Any]:
        del scenario_id
        raise AdmissionError("admission service is not configured")

    async def start_custom(
        self, submission: CustomRunSubmission
    ) -> dict[str, Any]:
        del submission
        raise AdmissionError("admission service is not configured")

    async def get_custom(self, run_id: str) -> dict[str, Any]:
        del run_id
        raise AdmissionError("admission service is not configured")


@cache
def configured_admission_client() -> AdmissionClient:
    service_url = os.getenv("ADMISSION_URL", "").strip()
    if not service_url:
        return UnconfiguredAdmissionClient()
    return CachedAdmissionClient(HttpAdmissionClient(service_url))
