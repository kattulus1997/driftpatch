from __future__ import annotations

import os
from functools import cache
from typing import Any, Protocol

from .schemas import AttemptLease, TaskRequest, WorkerProposal
from .service_client import ServiceRequestError, request_json


class ResultDeliveryError(RuntimeError):
    pass


class ResultPublisher(Protocol):
    async def preflight(self, task: TaskRequest) -> AttemptLease: ...

    async def publish(self, proposal: WorkerProposal) -> dict[str, Any]: ...


class HttpResultPublisher:
    def __init__(self, service_url: str) -> None:
        self._service_url = service_url.rstrip("/")

    async def preflight(self, task: TaskRequest) -> AttemptLease:
        try:
            result = await request_json(
                "POST",
                f"{self._service_url}/internal/attempts/preflight",
                audience=self._service_url,
                payload=task.model_dump(mode="json"),
            )
            return AttemptLease.model_validate(result)
        except (ServiceRequestError, ValueError) as exc:
            raise ResultDeliveryError("result service is unavailable") from exc

    async def publish(self, proposal: WorkerProposal) -> dict[str, Any]:
        try:
            return await request_json(
                "POST",
                f"{self._service_url}/internal/results",
                audience=self._service_url,
                payload=proposal.model_dump(mode="json"),
            )
        except ServiceRequestError as exc:
            raise ResultDeliveryError("result service is unavailable") from exc


class UnconfiguredResultPublisher:
    async def preflight(self, task: TaskRequest) -> AttemptLease:
        del task
        raise ResultDeliveryError("result service is not configured")

    async def publish(self, proposal: WorkerProposal) -> dict[str, Any]:
        del proposal
        raise ResultDeliveryError("result service is not configured")


@cache
def configured_result_publisher() -> ResultPublisher:
    service_url = os.getenv("RESULT_URL", "").strip()
    if not service_url:
        return UnconfiguredResultPublisher()
    return HttpResultPublisher(service_url)
