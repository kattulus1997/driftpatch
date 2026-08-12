from __future__ import annotations

import asyncio
import os
from collections import deque
from datetime import UTC, datetime
from functools import cache
from typing import Any
from uuid import uuid4

from .schemas import ValidationResult


_memory_runs: deque[dict[str, Any]] = deque(maxlen=100)
_memory_lock = asyncio.Lock()


def _document_id(event_id: str | None) -> str:
    candidate = event_id or str(uuid4())
    return "".join(character for character in candidate if character.isalnum() or character in "-_")[:120]


def _payload(
    result: ValidationResult, event_id: str | None, trigger: str | None
) -> dict[str, Any]:
    return {
        "id": _document_id(event_id),
        "recorded_at": datetime.now(UTC).isoformat(),
        "trigger": trigger or "api",
        **result.model_dump(mode="json"),
    }


@cache
def _firestore_client():
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project or not os.getenv("FIRESTORE_ENABLED"):
        return None
    from google.cloud import firestore

    return firestore.AsyncClient(
        project=project,
        database=os.getenv("FIRESTORE_DATABASE", "(default)"),
    )


async def save_run(
    result: ValidationResult, *, event_id: str | None, trigger: str | None
) -> dict[str, Any]:
    """Persist an idempotent evidence record in Firestore or the local ledger."""
    payload = _payload(result, event_id, trigger)
    client = _firestore_client()
    if client:
        await client.collection("driftpatch-runs").document(payload["id"]).set(payload)
        return payload

    async with _memory_lock:
        for index, existing in enumerate(_memory_runs):
            if existing["id"] == payload["id"]:
                _memory_runs[index] = payload
                break
        else:
            _memory_runs.appendleft(payload)
    return payload


async def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    client = _firestore_client()
    if client:
        query = (
            client.collection("driftpatch-runs")
            .order_by("recorded_at", direction="DESCENDING")
            .limit(limit)
        )
        return [snapshot.to_dict() async for snapshot in query.stream()]
    async with _memory_lock:
        return list(_memory_runs)[:limit]
