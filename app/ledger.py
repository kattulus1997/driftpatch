from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import cache
from typing import Any, Protocol
from uuid import uuid4

from .schemas import (
    AttemptLease,
    ConfigurationReceipt,
    CustomClaim,
    PipelineConfig,
    StoredBundle,
    ValidationResult,
)

TERMINAL_STATUSES = frozenset({"unchanged", "repaired", "escalated", "failed"})
MAX_PROPOSAL_FAILURES = 5
MAX_DISPATCH_ATTEMPTS = 2
_EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")


class ClaimDisposition(StrEnum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    TERMINAL = "terminal"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class EventClaim:
    disposition: ClaimDisposition
    attempt_id: str | None = None
    token: str | None = None


class EventStore(Protocol):
    async def claim(
        self,
        *,
        event_id: str,
        scenario_id: str,
        trigger: str,
        now: datetime,
        lease: timedelta,
        source_sha256: str | None = None,
    ) -> EventClaim: ...

    async def release(self, *, event_id: str, claim_token: str) -> bool: ...

    async def claim_custom(
        self,
        *,
        candidate_run_id: str,
        digest: str,
        uses_inference: bool,
        now: datetime,
        lease: timedelta,
        limit: int,
        total_limit: int | None = None,
    ) -> CustomClaim: ...

    async def attach_custom_bundle(
        self,
        *,
        run_id: str,
        claim_token: str,
        bundle: StoredBundle,
    ) -> bool: ...

    async def mark_custom_dispatched(
        self, *, run_id: str, claim_token: str
    ) -> bool: ...

    async def release_custom(
        self, *, run_id: str, claim_token: str
    ) -> bool: ...

    async def get_custom_bundle(self, run_id: str) -> StoredBundle | None: ...

    async def list_stale_custom(
        self, *, now: datetime, limit: int
    ) -> list[tuple[str, StoredBundle, str]]: ...

    async def terminalize_stale_custom(
        self,
        *,
        run_id: str,
        reason: str,
        result: ValidationResult,
        now: datetime,
    ) -> dict[str, Any] | None: ...

    async def preflight(
        self,
        *,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        attempt_token: str,
        now: datetime,
        lease: timedelta,
    ) -> AttemptLease: ...

    async def commit_terminal(
        self,
        *,
        result: ValidationResult,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        execution_token: str,
        trigger: str,
        now: datetime,
        base_configuration: PipelineConfig | None = None,
        candidate_configuration: PipelineConfig | None = None,
        affected_outputs: tuple[str, ...] = (),
        require_fresh_lease: bool = False,
    ) -> dict[str, Any]: ...

    async def reject_proposal(
        self,
        *,
        result: ValidationResult,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        execution_token: str,
        trigger: str,
        now: datetime,
        require_fresh_lease: bool = False,
    ) -> dict[str, Any] | None: ...

    async def get_record(self, event_id: str) -> dict[str, Any] | None: ...

    async def get_terminal(self, event_id: str) -> dict[str, Any] | None: ...

    async def get_active_configuration(
        self, scenario_id: str
    ) -> dict[str, Any] | None: ...

    async def list_terminal(self, limit: int) -> list[dict[str, Any]]: ...

    async def is_terminal(self, event_id: str) -> bool: ...


def _document_id(event_id: str | None) -> str:
    candidate = event_id or str(uuid4())
    if not _EVENT_ID.fullmatch(candidate):
        raise ValueError("event_id must contain 1-120 letters, numbers, '-' or '_'")
    return candidate


def _terminal_payload(
    result: ValidationResult,
    *,
    event_id: str,
    trigger: str,
    now: datetime,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    payload = {
        "id": event_id,
        "recorded_at": now.astimezone(UTC).isoformat(),
        "trigger": trigger,
        **result.model_dump(mode="json"),
    }
    if source_sha256:
        payload["source_sha256"] = source_sha256
    return payload


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if not key.startswith("_") and key != "lease_expires_at"
    }


def _stale_custom_reason(
    record: dict[str, Any], now: datetime
) -> str | None:
    if (
        record.get("status") != "processing"
        or not re.fullmatch(r"custom_[0-9a-f]{32}", str(record.get("scenario_id", "")))
        or not record.get("_bundle")
    ):
        return None
    execution_expires = record.get("_execution_lease_expires_at")
    if isinstance(execution_expires, datetime) and execution_expires <= now:
        return "execution_exhausted"
    admission_expires = record.get("lease_expires_at")
    if isinstance(admission_expires, datetime) and admission_expires <= now:
        return "delivery_exhausted"
    return None


def _configuration_sha256(configuration: PipelineConfig) -> str:
    encoded = json.dumps(
        configuration.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _prepare_configuration_application(
    *,
    result: ValidationResult,
    event_id: str,
    now: datetime,
    current: dict[str, Any] | None,
    base: PipelineConfig | None,
    candidate: PipelineConfig | None,
    affected_outputs: tuple[str, ...],
) -> tuple[ValidationResult, dict[str, Any] | None, dict[str, Any] | None]:
    if result.status != "repaired":
        return result, None, None
    if base is None or candidate is None:
        raise RuntimeError("repaired results require a validated configuration")

    base_sha256 = _configuration_sha256(base)
    candidate_sha256 = _configuration_sha256(candidate)
    current_sha256 = current.get("applied_sha256") if current else base_sha256
    if current_sha256 == candidate_sha256:
        receipt = ConfigurationReceipt(
            state="already_active",
            version=int(current["version"]),
            affected_outputs=list(affected_outputs),
            previous_sha256=current["previous_sha256"],
            applied_sha256=candidate_sha256,
            rollback_ready=True,
        )
        return result.model_copy(update={"application": receipt}), None, None
    if current_sha256 != base_sha256:
        raise RuntimeError("active configuration changed after proposal evaluation")

    version = int(current.get("version", 0)) + 1 if current else 1
    previous_configuration = (
        copy.deepcopy(current["configuration"])
        if current
        else base.model_dump(mode="json")
    )
    active = {
        "scenario_id": result.scenario_id,
        "version": version,
        "configuration": candidate.model_dump(mode="json"),
        "previous_configuration": previous_configuration,
        "previous_sha256": current_sha256,
        "applied_sha256": candidate_sha256,
        "source_event_id": event_id,
        "affected_outputs": list(affected_outputs),
        "applied_at": now.astimezone(UTC).isoformat(),
    }
    history = {
        **active,
        "configuration": copy.deepcopy(active["configuration"]),
        "previous_configuration": copy.deepcopy(previous_configuration),
    }
    receipt = ConfigurationReceipt(
        state="applied",
        version=version,
        affected_outputs=list(affected_outputs),
        previous_sha256=current_sha256,
        applied_sha256=candidate_sha256,
        rollback_ready=True,
    )
    return result.model_copy(update={"application": receipt}), active, history


class InMemoryEventStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._configurations: dict[str, dict[str, Any]] = {}
        self._configuration_history: dict[str, dict[str, Any]] = {}
        self._custom_dedupe: dict[tuple[str, str], str] = {}
        self._custom_quotas: dict[str, int] = {}
        self._custom_total_quotas: dict[str, int] = {}
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def claim(
        self,
        *,
        event_id: str,
        scenario_id: str,
        trigger: str,
        now: datetime,
        lease: timedelta,
        source_sha256: str | None = None,
    ) -> EventClaim:
        identifier = _document_id(event_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if existing and existing.get("scenario_id") != scenario_id:
                raise ValueError("event_id is already bound to another scenario")
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return EventClaim(ClaimDisposition.TERMINAL)
            if existing and existing.get("lease_expires_at", now) > now:
                return EventClaim(ClaimDisposition.IN_PROGRESS)
            dispatch_attempts = (
                int(existing.get("_dispatch_attempts", 0)) if existing else 0
            )
            if dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
                return EventClaim(ClaimDisposition.EXHAUSTED)

            attempt_id = str(uuid4())
            token = str(uuid4())
            self._records[identifier] = {
                "id": identifier,
                "scenario_id": scenario_id,
                "status": "processing",
                "trigger": existing.get("trigger", trigger) if existing else trigger,
                "claimed_at": now.astimezone(UTC).isoformat(),
                "lease_expires_at": now + lease,
                "_attempt_id": attempt_id,
                "_attempt_token": token,
                "_dispatch_attempts": dispatch_attempts + 1,
                "_proposal_failures": (
                    existing.get("_proposal_failures", 0) if existing else 0
                ),
            }
            observed_digest = source_sha256 or (
                existing.get("source_sha256") if existing else None
            )
            if observed_digest:
                self._records[identifier]["source_sha256"] = observed_digest
            return EventClaim(ClaimDisposition.ACQUIRED, attempt_id, token)

    async def release(self, *, event_id: str, claim_token: str) -> bool:
        identifier = _document_id(event_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("_attempt_token") != claim_token
            ):
                return False
            existing.update(
                {
                    "lease_expires_at": datetime(1970, 1, 1, tzinfo=UTC),
                    "_attempt_id": None,
                    "_attempt_token": None,
                    "_execution_token": None,
                    "_execution_lease_expires_at": None,
                }
            )
            return True

    async def claim_custom(
        self,
        *,
        candidate_run_id: str,
        digest: str,
        uses_inference: bool,
        now: datetime,
        lease: timedelta,
        limit: int,
        total_limit: int | None = None,
    ) -> CustomClaim:
        identifier = _document_id(candidate_run_id)
        if not re.fullmatch(r"custom_[0-9a-f]{32}", identifier):
            raise ValueError("custom run identifier is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("custom digest is invalid")
        if limit < 1:
            raise ValueError("custom daily limit must be positive")
        total_limit = limit * 2 if total_limit is None else total_limit
        if total_limit < 1:
            raise ValueError("custom total daily limit must be positive")
        day = now.astimezone(UTC).date().isoformat()
        dedupe_key = (day, digest)
        async with self._lock:
            existing_id = self._custom_dedupe.get(dedupe_key)
            if existing_id is not None:
                return CustomClaim(disposition="existing", run_id=existing_id)
            used = self._custom_quotas.get(day, 0)
            total_used = self._custom_total_quotas.get(day, 0)
            if total_used >= total_limit:
                return CustomClaim(
                    disposition="exhausted",
                    run_id=identifier,
                    exhausted_budget="total",
                )
            if uses_inference and used >= limit:
                return CustomClaim(
                    disposition="exhausted",
                    run_id=identifier,
                    exhausted_budget="inference",
                )

            attempt_id = str(uuid4())
            token = str(uuid4())
            self._custom_dedupe[dedupe_key] = identifier
            self._custom_total_quotas[day] = total_used + 1
            if uses_inference:
                self._custom_quotas[day] = used + 1
            self._records[identifier] = {
                "id": identifier,
                "scenario_id": identifier,
                "status": "admitting",
                "trigger": "public-custom",
                "claimed_at": now.astimezone(UTC).isoformat(),
                "lease_expires_at": now + lease,
                "_attempt_id": attempt_id,
                "_attempt_token": token,
                "_dispatch_attempts": 1,
                "_proposal_failures": 0,
                "_custom_digest": digest,
                "_custom_day": day,
                "_uses_inference": uses_inference,
                "_bundle": None,
                "_dispatched": False,
            }
            return CustomClaim(
                disposition="acquired",
                run_id=identifier,
                attempt_id=attempt_id,
                token=token,
            )

    async def attach_custom_bundle(
        self,
        *,
        run_id: str,
        claim_token: str,
        bundle: StoredBundle,
    ) -> bool:
        identifier = _document_id(run_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if (
                not existing
                or existing.get("status") != "admitting"
                or existing.get("_attempt_token") != claim_token
                or existing.get("_bundle") is not None
            ):
                return False
            existing["_bundle"] = bundle.model_dump(mode="json")
            existing["status"] = "processing"
            return True

    async def mark_custom_dispatched(
        self, *, run_id: str, claim_token: str
    ) -> bool:
        identifier = _document_id(run_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("_attempt_token") != claim_token
                or existing.get("_bundle") is None
            ):
                return False
            existing["_dispatched"] = True
            return True

    async def release_custom(
        self, *, run_id: str, claim_token: str
    ) -> bool:
        identifier = _document_id(run_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if (
                not existing
                or existing.get("_attempt_token") != claim_token
                or existing.get("_dispatched")
            ):
                return False
            day = existing["_custom_day"]
            digest = existing["_custom_digest"]
            self._custom_dedupe.pop((day, digest), None)
            self._custom_total_quotas[day] = max(
                0, self._custom_total_quotas.get(day, 0) - 1
            )
            if existing.get("_uses_inference"):
                self._custom_quotas[day] = max(
                    0, self._custom_quotas.get(day, 0) - 1
                )
            del self._records[identifier]
            return True

    async def get_custom_bundle(self, run_id: str) -> StoredBundle | None:
        identifier = _document_id(run_id)
        if not re.fullmatch(r"custom_[0-9a-f]{32}", identifier):
            raise ValueError("custom run identifier is invalid")
        async with self._lock:
            existing = self._records.get(identifier)
            payload = existing.get("_bundle") if existing else None
            return StoredBundle.model_validate(payload) if payload else None

    async def list_stale_custom(
        self, *, now: datetime, limit: int
    ) -> list[tuple[str, StoredBundle, str]]:
        if limit < 1:
            return []
        async with self._lock:
            stale = []
            for identifier, record in self._records.items():
                reason = _stale_custom_reason(record, now)
                if reason is None:
                    continue
                stale.append(
                    (
                        identifier,
                        StoredBundle.model_validate(record["_bundle"]),
                        reason,
                    )
                )
                if len(stale) >= limit:
                    break
            return stale

    async def terminalize_stale_custom(
        self,
        *,
        run_id: str,
        reason: str,
        result: ValidationResult,
        now: datetime,
    ) -> dict[str, Any] | None:
        identifier = _document_id(run_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if not existing or existing.get("status") in TERMINAL_STATUSES:
                return None
            if _stale_custom_reason(existing, now) != reason:
                return None
            if (
                result.scenario_id != identifier
                or result.status != "escalated"
                or result.program is None
                or result.program.rationale != reason
            ):
                raise ValueError("stale terminal result does not match the run")
            payload = _terminal_payload(
                result,
                event_id=identifier,
                trigger=existing.get("trigger", "reconciler"),
                now=now,
                source_sha256=existing.get("source_sha256"),
            )
            self._records[identifier] = payload
            return payload.copy()

    async def preflight(
        self,
        *,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        attempt_token: str,
        now: datetime,
        lease: timedelta,
    ) -> AttemptLease:
        identifier = _document_id(event_id)
        async with self._lock:
            existing = self._records.get(identifier)
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return AttemptLease(disposition="terminal")
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("scenario_id") != scenario_id
                or existing.get("_attempt_id") != attempt_id
                or existing.get("_attempt_token") != attempt_token
                or existing.get("lease_expires_at", now) <= now
            ):
                return AttemptLease(disposition="stale")
            execution_expires = existing.get("_execution_lease_expires_at")
            if execution_expires is not None and execution_expires > now:
                return AttemptLease(disposition="busy")
            execution_token = str(uuid4())
            existing["_execution_token"] = execution_token
            existing["_execution_lease_expires_at"] = now + lease
            return AttemptLease(
                disposition="run",
                execution_token=execution_token,
            )

    async def commit_terminal(
        self,
        *,
        result: ValidationResult,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        execution_token: str,
        trigger: str,
        now: datetime,
        base_configuration: PipelineConfig | None = None,
        candidate_configuration: PipelineConfig | None = None,
        affected_outputs: tuple[str, ...] = (),
        require_fresh_lease: bool = False,
    ) -> dict[str, Any]:
        identifier = _document_id(event_id)
        async with self._lock:
            commit_time = (
                self._clock().astimezone(UTC) if require_fresh_lease else now
            )
            existing = self._records.get(identifier)
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return _public_record(existing)
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("scenario_id") != scenario_id
                or existing.get("_attempt_id") != attempt_id
                or existing.get("_execution_token") != execution_token
                or existing.get("_execution_lease_expires_at") is None
                or existing["_execution_lease_expires_at"] <= commit_time
                or result.scenario_id != scenario_id
            ):
                raise RuntimeError("terminal commit requires the active execution")
            if result.status == "failed":
                raise RuntimeError("failed proposals require bounded rejection handling")
            result, active_configuration, history = _prepare_configuration_application(
                result=result,
                event_id=identifier,
                now=commit_time,
                current=self._configurations.get(scenario_id),
                base=base_configuration,
                candidate=candidate_configuration,
                affected_outputs=affected_outputs,
            )
            payload = _terminal_payload(
                result,
                event_id=identifier,
                trigger=existing.get("trigger", trigger),
                now=commit_time,
                source_sha256=existing.get("source_sha256"),
            )
            if active_configuration is not None and history is not None:
                self._configurations[scenario_id] = active_configuration
                self._configuration_history[identifier] = history
            self._records[identifier] = payload
            return payload.copy()

    async def reject_proposal(
        self,
        *,
        result: ValidationResult,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        execution_token: str,
        trigger: str,
        now: datetime,
        require_fresh_lease: bool = False,
    ) -> dict[str, Any] | None:
        identifier = _document_id(event_id)
        async with self._lock:
            commit_time = (
                self._clock().astimezone(UTC) if require_fresh_lease else now
            )
            existing = self._records.get(identifier)
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return _public_record(existing)
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("scenario_id") != scenario_id
                or existing.get("_attempt_id") != attempt_id
                or existing.get("_execution_token") != execution_token
                or existing.get("_execution_lease_expires_at") is None
                or existing["_execution_lease_expires_at"] <= commit_time
                or result.scenario_id != scenario_id
                or result.status != "failed"
            ):
                raise RuntimeError("proposal rejection requires the active execution")
            failures = int(existing.get("_proposal_failures", 0)) + 1
            if failures < MAX_PROPOSAL_FAILURES:
                existing["_proposal_failures"] = failures
                existing["_execution_token"] = None
                existing["_execution_lease_expires_at"] = None
                return None
            payload = _terminal_payload(
                result,
                event_id=identifier,
                trigger=existing.get("trigger", trigger),
                now=commit_time,
                source_sha256=existing.get("source_sha256"),
            )
            self._records[identifier] = payload
            return payload.copy()

    async def get_record(self, event_id: str) -> dict[str, Any] | None:
        identifier = _document_id(event_id)
        async with self._lock:
            record = self._records.get(identifier)
            return _public_record(record) if record else None

    async def get_terminal(self, event_id: str) -> dict[str, Any] | None:
        record = await self.get_record(event_id)
        return record if record and record.get("status") in TERMINAL_STATUSES else None

    async def get_active_configuration(
        self, scenario_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            configuration = self._configurations.get(scenario_id)
            return copy.deepcopy(configuration) if configuration else None

    async def list_terminal(self, limit: int) -> list[dict[str, Any]]:
        async with self._lock:
            records = [
                _public_record(record)
                for record in self._records.values()
                if record.get("status") in TERMINAL_STATUSES
            ]
        return sorted(
            records,
            key=lambda record: record.get("recorded_at", ""),
            reverse=True,
        )[:limit]

    async def is_terminal(self, event_id: str) -> bool:
        return await self.get_terminal(event_id) is not None

class FirestoreEventStore:
    def __init__(
        self, client: Any, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._collection = client.collection("driftpatch-runs")
        self._configurations = client.collection("driftpatch-configurations")
        self._configuration_history = client.collection(
            "driftpatch-configuration-history"
        )
        self._custom_dedupe = client.collection("driftpatch-custom-dedupe")
        self._custom_quotas = client.collection("driftpatch-custom-quotas")

    async def claim(
        self,
        *,
        event_id: str,
        scenario_id: str,
        trigger: str,
        now: datetime,
        lease: timedelta,
        source_sha256: str | None = None,
    ) -> EventClaim:
        from google.cloud import firestore

        identifier = _document_id(event_id)
        reference = self._collection.document(identifier)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def acquire(active_transaction: Any) -> EventClaim:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if existing and existing.get("scenario_id") != scenario_id:
                raise ValueError("event_id is already bound to another scenario")
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return EventClaim(ClaimDisposition.TERMINAL)
            if existing and existing.get("lease_expires_at", now) > now:
                return EventClaim(ClaimDisposition.IN_PROGRESS)
            dispatch_attempts = (
                int(existing.get("_dispatch_attempts", 0)) if existing else 0
            )
            if dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
                return EventClaim(ClaimDisposition.EXHAUSTED)

            attempt_id = str(uuid4())
            token = str(uuid4())
            payload = {
                "id": identifier,
                "scenario_id": scenario_id,
                "status": "processing",
                "trigger": existing.get("trigger", trigger) if existing else trigger,
                "claimed_at": now,
                "lease_expires_at": now + lease,
                "_attempt_id": attempt_id,
                "_attempt_token": token,
                "_dispatch_attempts": dispatch_attempts + 1,
                "_proposal_failures": (
                    existing.get("_proposal_failures", 0) if existing else 0
                ),
            }
            observed_digest = source_sha256 or (
                existing.get("source_sha256") if existing else None
            )
            if observed_digest:
                payload["source_sha256"] = observed_digest
            active_transaction.set(reference, payload)
            return EventClaim(ClaimDisposition.ACQUIRED, attempt_id, token)

        return await acquire(transaction)

    async def release(self, *, event_id: str, claim_token: str) -> bool:
        from google.cloud import firestore

        reference = self._collection.document(_document_id(event_id))
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def abandon(active_transaction: Any) -> bool:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("_attempt_token") != claim_token
            ):
                return False
            active_transaction.update(
                reference,
                {
                    "lease_expires_at": datetime(1970, 1, 1, tzinfo=UTC),
                    "_attempt_id": None,
                    "_attempt_token": None,
                    "_execution_token": None,
                    "_execution_lease_expires_at": None,
                },
            )
            return True

        return await abandon(transaction)

    async def claim_custom(
        self,
        *,
        candidate_run_id: str,
        digest: str,
        uses_inference: bool,
        now: datetime,
        lease: timedelta,
        limit: int,
        total_limit: int | None = None,
    ) -> CustomClaim:
        from google.cloud import firestore

        identifier = _document_id(candidate_run_id)
        if not re.fullmatch(r"custom_[0-9a-f]{32}", identifier):
            raise ValueError("custom run identifier is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("custom digest is invalid")
        if limit < 1:
            raise ValueError("custom daily limit must be positive")
        total_limit = limit * 2 if total_limit is None else total_limit
        if total_limit < 1:
            raise ValueError("custom total daily limit must be positive")
        day = now.astimezone(UTC).date().isoformat()
        record_reference = self._collection.document(identifier)
        dedupe_reference = self._custom_dedupe.document(f"{day}-{digest}")
        quota_reference = self._custom_quotas.document(day)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def reserve(active_transaction: Any) -> CustomClaim:
            dedupe_snapshot = await dedupe_reference.get(
                transaction=active_transaction
            )
            if dedupe_snapshot.exists:
                existing = dedupe_snapshot.to_dict()
                return CustomClaim(
                    disposition="existing", run_id=existing["run_id"]
                )
            quota_snapshot = await quota_reference.get(transaction=active_transaction)
            record_snapshot = await record_reference.get(
                transaction=active_transaction
            )
            if record_snapshot.exists:
                raise RuntimeError("custom run identifier collision")
            quota = quota_snapshot.to_dict() if quota_snapshot.exists else {}
            used = int(quota.get("inference_runs", 0))
            total_used = int(quota.get("total_runs", 0))
            if total_used >= total_limit:
                return CustomClaim(
                    disposition="exhausted",
                    run_id=identifier,
                    exhausted_budget="total",
                )
            if uses_inference and used >= limit:
                return CustomClaim(
                    disposition="exhausted",
                    run_id=identifier,
                    exhausted_budget="inference",
                )

            attempt_id = str(uuid4())
            token = str(uuid4())
            active_transaction.set(
                record_reference,
                {
                    "id": identifier,
                    "scenario_id": identifier,
                    "status": "admitting",
                    "trigger": "public-custom",
                    "claimed_at": now,
                    "lease_expires_at": now + lease,
                    "_attempt_id": attempt_id,
                    "_attempt_token": token,
                    "_dispatch_attempts": 1,
                    "_proposal_failures": 0,
                    "_custom_digest": digest,
                    "_custom_day": day,
                    "_uses_inference": uses_inference,
                    "_bundle": None,
                    "_dispatched": False,
                },
            )
            active_transaction.set(
                dedupe_reference,
                {"run_id": identifier, "created_at": now},
            )
            active_transaction.set(
                quota_reference,
                {
                    "inference_runs": used + int(uses_inference),
                    "total_runs": total_used + 1,
                    "updated_at": now,
                },
            )
            return CustomClaim(
                disposition="acquired",
                run_id=identifier,
                attempt_id=attempt_id,
                token=token,
            )

        return await reserve(transaction)

    async def attach_custom_bundle(
        self,
        *,
        run_id: str,
        claim_token: str,
        bundle: StoredBundle,
    ) -> bool:
        from google.cloud import firestore

        reference = self._collection.document(_document_id(run_id))
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def attach(active_transaction: Any) -> bool:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if (
                not existing
                or existing.get("status") != "admitting"
                or existing.get("_attempt_token") != claim_token
                or existing.get("_bundle") is not None
            ):
                return False
            active_transaction.update(
                reference,
                {
                    "_bundle": bundle.model_dump(mode="json"),
                    "status": "processing",
                },
            )
            return True

        return await attach(transaction)

    async def mark_custom_dispatched(
        self, *, run_id: str, claim_token: str
    ) -> bool:
        from google.cloud import firestore

        reference = self._collection.document(_document_id(run_id))
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def mark(active_transaction: Any) -> bool:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("_attempt_token") != claim_token
                or existing.get("_bundle") is None
            ):
                return False
            active_transaction.update(reference, {"_dispatched": True})
            return True

        return await mark(transaction)

    async def release_custom(
        self, *, run_id: str, claim_token: str
    ) -> bool:
        from google.cloud import firestore

        reference = self._collection.document(_document_id(run_id))
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def abandon_custom(active_transaction: Any) -> bool:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if (
                not existing
                or existing.get("_attempt_token") != claim_token
                or existing.get("_dispatched")
            ):
                return False
            day = existing["_custom_day"]
            digest = existing["_custom_digest"]
            dedupe_reference = self._custom_dedupe.document(f"{day}-{digest}")
            quota_reference = self._custom_quotas.document(day)
            dedupe_snapshot = await dedupe_reference.get(
                transaction=active_transaction
            )
            quota_snapshot = await quota_reference.get(transaction=active_transaction)
            quota = quota_snapshot.to_dict() if quota_snapshot.exists else {}
            active_transaction.delete(reference)
            if dedupe_snapshot.exists:
                active_transaction.delete(dedupe_reference)
            active_transaction.set(
                quota_reference,
                {
                    "inference_runs": max(
                        0,
                        int(quota.get("inference_runs", 0))
                        - int(bool(existing.get("_uses_inference"))),
                    ),
                    "total_runs": max(0, int(quota.get("total_runs", 0)) - 1),
                    "updated_at": datetime.now(UTC),
                },
            )
            return True

        return await abandon_custom(transaction)

    async def get_custom_bundle(self, run_id: str) -> StoredBundle | None:
        identifier = _document_id(run_id)
        if not re.fullmatch(r"custom_[0-9a-f]{32}", identifier):
            raise ValueError("custom run identifier is invalid")
        snapshot = await self._collection.document(identifier).get()
        existing = snapshot.to_dict() if snapshot.exists else None
        payload = existing.get("_bundle") if existing else None
        return StoredBundle.model_validate(payload) if payload else None

    async def list_stale_custom(
        self, *, now: datetime, limit: int
    ) -> list[tuple[str, StoredBundle, str]]:
        if limit < 1:
            return []
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = (
            self._collection.where(filter=FieldFilter("status", "==", "processing"))
            .limit(max(limit * 4, limit))
        )
        stale: list[tuple[str, StoredBundle, str]] = []
        async for snapshot in query.stream():
            record = snapshot.to_dict()
            reason = _stale_custom_reason(record, now)
            if reason is None:
                continue
            stale.append(
                (
                    snapshot.id,
                    StoredBundle.model_validate(record["_bundle"]),
                    reason,
                )
            )
            if len(stale) >= limit:
                break
        return stale

    async def terminalize_stale_custom(
        self,
        *,
        run_id: str,
        reason: str,
        result: ValidationResult,
        now: datetime,
    ) -> dict[str, Any] | None:
        from google.cloud import firestore

        identifier = _document_id(run_id)
        reference = self._collection.document(identifier)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def terminalize(active_transaction: Any) -> dict[str, Any] | None:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if not existing or existing.get("status") in TERMINAL_STATUSES:
                return None
            if _stale_custom_reason(existing, now) != reason:
                return None
            if (
                result.scenario_id != identifier
                or result.status != "escalated"
                or result.program is None
                or result.program.rationale != reason
            ):
                raise ValueError("stale terminal result does not match the run")
            payload = _terminal_payload(
                result,
                event_id=identifier,
                trigger=existing.get("trigger", "reconciler"),
                now=now,
                source_sha256=existing.get("source_sha256"),
            )
            active_transaction.set(reference, payload)
            return payload

        return await terminalize(transaction)

    async def preflight(
        self,
        *,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        attempt_token: str,
        now: datetime,
        lease: timedelta,
    ) -> AttemptLease:
        from google.cloud import firestore

        reference = self._collection.document(_document_id(event_id))
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def acquire_execution(active_transaction: Any) -> AttemptLease:
            snapshot = await reference.get(transaction=active_transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return AttemptLease(disposition="terminal")
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("scenario_id") != scenario_id
                or existing.get("_attempt_id") != attempt_id
                or existing.get("_attempt_token") != attempt_token
                or existing.get("lease_expires_at", now) <= now
            ):
                return AttemptLease(disposition="stale")
            execution_expires = existing.get("_execution_lease_expires_at")
            if execution_expires is not None and execution_expires > now:
                return AttemptLease(disposition="busy")
            execution_token = str(uuid4())
            active_transaction.update(
                reference,
                {
                    "_execution_token": execution_token,
                    "_execution_lease_expires_at": now + lease,
                },
            )
            return AttemptLease(
                disposition="run",
                execution_token=execution_token,
            )

        return await acquire_execution(transaction)

    async def commit_terminal(
        self,
        *,
        result: ValidationResult,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        execution_token: str,
        trigger: str,
        now: datetime,
        base_configuration: PipelineConfig | None = None,
        candidate_configuration: PipelineConfig | None = None,
        affected_outputs: tuple[str, ...] = (),
        require_fresh_lease: bool = False,
    ) -> dict[str, Any]:
        from google.cloud import firestore

        identifier = _document_id(event_id)
        reference = self._collection.document(identifier)
        configuration_reference = self._configurations.document(scenario_id)
        history_reference = self._configuration_history.document(identifier)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def commit(active_transaction: Any) -> dict[str, Any]:
            snapshot = await reference.get(transaction=active_transaction)
            configuration_snapshot = await configuration_reference.get(
                transaction=active_transaction
            )
            commit_time = (
                self._clock().astimezone(UTC) if require_fresh_lease else now
            )
            existing = snapshot.to_dict() if snapshot.exists else None
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return _public_record(existing)
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("scenario_id") != scenario_id
                or existing.get("_attempt_id") != attempt_id
                or existing.get("_execution_token") != execution_token
                or existing.get("_execution_lease_expires_at") is None
                or existing["_execution_lease_expires_at"] <= commit_time
                or result.scenario_id != scenario_id
            ):
                raise RuntimeError("terminal commit requires the active execution")
            if result.status == "failed":
                raise RuntimeError("failed proposals require bounded rejection handling")
            current_configuration = (
                configuration_snapshot.to_dict()
                if configuration_snapshot.exists
                else None
            )
            verified_result, active_configuration, history = (
                _prepare_configuration_application(
                    result=result,
                    event_id=identifier,
                    now=commit_time,
                    current=current_configuration,
                    base=base_configuration,
                    candidate=candidate_configuration,
                    affected_outputs=affected_outputs,
                )
            )
            payload = _terminal_payload(
                verified_result,
                event_id=identifier,
                trigger=existing.get("trigger", trigger),
                now=commit_time,
                source_sha256=existing.get("source_sha256"),
            )
            if active_configuration is not None and history is not None:
                active_transaction.set(configuration_reference, active_configuration)
                active_transaction.set(history_reference, history)
            active_transaction.set(reference, payload)
            return payload

        return await commit(transaction)

    async def reject_proposal(
        self,
        *,
        result: ValidationResult,
        event_id: str,
        scenario_id: str,
        attempt_id: str,
        execution_token: str,
        trigger: str,
        now: datetime,
        require_fresh_lease: bool = False,
    ) -> dict[str, Any] | None:
        from google.cloud import firestore

        identifier = _document_id(event_id)
        reference = self._collection.document(identifier)
        transaction = self._client.transaction(max_attempts=5)

        @firestore.async_transactional
        async def reject(active_transaction: Any) -> dict[str, Any] | None:
            snapshot = await reference.get(transaction=active_transaction)
            commit_time = (
                self._clock().astimezone(UTC) if require_fresh_lease else now
            )
            existing = snapshot.to_dict() if snapshot.exists else None
            if existing and existing.get("status") in TERMINAL_STATUSES:
                return _public_record(existing)
            if (
                not existing
                or existing.get("status") != "processing"
                or existing.get("scenario_id") != scenario_id
                or existing.get("_attempt_id") != attempt_id
                or existing.get("_execution_token") != execution_token
                or existing.get("_execution_lease_expires_at") is None
                or existing["_execution_lease_expires_at"] <= commit_time
                or result.scenario_id != scenario_id
                or result.status != "failed"
            ):
                raise RuntimeError("proposal rejection requires the active execution")
            failures = int(existing.get("_proposal_failures", 0)) + 1
            if failures < MAX_PROPOSAL_FAILURES:
                active_transaction.update(
                    reference,
                    {
                        "_proposal_failures": failures,
                        "_execution_token": None,
                        "_execution_lease_expires_at": None,
                    },
                )
                return None
            payload = _terminal_payload(
                result,
                event_id=identifier,
                trigger=existing.get("trigger", trigger),
                now=commit_time,
                source_sha256=existing.get("source_sha256"),
            )
            active_transaction.set(reference, payload)
            return payload

        return await reject(transaction)

    async def get_record(self, event_id: str) -> dict[str, Any] | None:
        reference = self._collection.document(_document_id(event_id))
        snapshot = await reference.get()
        record = snapshot.to_dict() if snapshot.exists else None
        return _public_record(record) if record else None

    async def get_terminal(self, event_id: str) -> dict[str, Any] | None:
        record = await self.get_record(event_id)
        return record if record and record.get("status") in TERMINAL_STATUSES else None

    async def get_active_configuration(
        self, scenario_id: str
    ) -> dict[str, Any] | None:
        snapshot = await self._configurations.document(scenario_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def list_terminal(self, limit: int) -> list[dict[str, Any]]:
        query = (
            self._collection.where("status", "in", sorted(TERMINAL_STATUSES))
            .order_by("recorded_at", direction="DESCENDING")
            .limit(limit)
        )
        return [_public_record(snapshot.to_dict()) async for snapshot in query.stream()]

    async def is_terminal(self, event_id: str) -> bool:
        return await self.get_terminal(event_id) is not None


def _firestore_enabled() -> bool:
    value = os.getenv("FIRESTORE_ENABLED")
    if value is None or not value.strip():
        return False
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise RuntimeError("FIRESTORE_ENABLED must be 'true' or 'false'")


@cache
def configured_event_store() -> EventStore:
    if not _firestore_enabled():
        return InMemoryEventStore()
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required when Firestore is enabled")
    from google.cloud import firestore

    client = firestore.AsyncClient(
        project=project,
        database=os.getenv("FIRESTORE_DATABASE", "(default)"),
    )
    return FirestoreEventStore(client)
